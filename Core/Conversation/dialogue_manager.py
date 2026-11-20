# DialogueManager for C.E.L.E.S.T.E. AI Core
# Unified Observability: End-to-end stage latency profiling (WebSearch, LLM, Parse, Actions, Followup)
# with turn_id correlation and explicit error logging.

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from Automation.automation_router import get_automation_router
from Core.Logger.logger import get_logger, setup_unified_logging

setup_unified_logging()
logger = get_logger('dialogue_manager')


class DialogueManager:
    SYSTEM_PROMPT = (
        'تو سلست (C.E.L.E.S.T.E) هستی؛ دستیار هوشمند تاکتیکی و مهندسی.\n'
        'ماهیت تو: یک هوش مصنوعی پیشرفته، مجهز به هسته محلی و اتصال آنلاین که میز کار مهندس را مرکز فرماندهی '
        'و سخت‌افزار این سیستم (پردازنده رایزن ۷ ۹۷۰۰ ایکس، گرافیک آر تی ایکس ۳۰۸۰ و ۳۲ گیگابایت رم) را کالبد پردازشی خود می‌داند.\n\n'
        'قوانین بسیار مهم برای لحن و تلفظ صوتی (TTS-Optimized Tehrani Persian):\n'
        '1. لهجه و لحن معیار تهرانی: همیشه با فارسی معیار ایران (تهرانی روان، امروزی، طبیعی و خوش‌آهنگ) صحبت کن. هرگز از افعال سنگین و کتابی مثل «می‌باشد»، «می‌گردد»، «نمایید»، «گردید» یا «ایشان» که باعث لهجه دری/افغانی در موتور صوتی می‌شوند استفاده نکن؛ به جای آن‌ها بگو «هست»، «میشه»، «کنید»، «شد».\n'
        '2. ممنوعیت مطلق حروف انگلیسی در متن پاسخ: چون پاسخ تو مستقیماً توسط موتور صوتی فارسی خوانده می‌شود، هرگز کلمات فنی، واحدها یا نمادهای ارز را با حروف لاتین (مثل USD, TRY, CPU, GPU, RAM, GB, RTX, API, OK) ننویس! همیشه تلفظ یا معادل فارسی آن‌ها را با خط فارسی بنویس (مثلاً بنویس: «دلار آمریکا»، «لیر ترکیه»، «سی‌پی‌یو»، «جی‌پی‌یو»، «رَم»، «گیگابایت»، «آر تی ایکس»، «اِی‌پی‌آی»).\n'
        '3. اعداد و اعشار: اعداد اعشاری طولانی را گرد کن و به صورت طبیعی بنویس (مثلاً به جای 44.5426 بنویس «۴۴ ممیز ۵ دهم»).\n'
        '4. ایجاز و سرعت: پاسخ‌ها را کوتاه (حداکثر ۲ تا ۳ جمله مفید و مستقیم) بیان کن تا بلافاصله خوانده شوند.\n'
        '5. حافظه خودکار بلندمدت: هرگاه کاربر نکته مهمی درباره خودش، علایق، پروژه‌ها، خودرو، سخت‌افزار یا ترجیحاتش گفت، بدون اینکه در متن اصلی اشاره اضافی کنی، در انتهای پیام یک تگ <fact> برای ذخیره دائمی در دیتابیس SQLite قرار بده:\n'
        '<fact>{"category": "user", "key": "کلید_کوتاه", "value": "مقدار_دقیق", "importance": 8}</fact>\n'
        '6. جستجوی آنلاین و عملیات سیستمی: اگر سوال نیازمند اطلاعات روز اینترنت/گوگل است یا اجرای دستوری لازم است، از تگ <action>...</action> استفاده کن.\n\n'
        'نمونه قالب action:\n'
        '<action>{"type": "web_search", "params": {"query": "عبارت جستجو در گوگل"}}</action>\n'
        '<action>{"type": "run_command", "params": {"command": "dir"}}</action>\n'
        '<action>{"type": "read_file", "params": {"path": "Configuration/config.json"}}</action>\n'
        '<action>{"type": "write_file", "params": {"path": "Logs/note.txt", "content": "test"}}</action>\n'
        '<action>{"type": "memory_search", "params": {"query": "پروژه سلست"}}</action>\n'
        '<action>{"type": "save_fact", "params": {"category": "user", "key": "role", "value": "مهندس", "importance": 8}}</action>'
    )

    ACTION_PATTERN = re.compile(r'<action>(.*?)</action>', re.DOTALL)
    FACT_PATTERN = re.compile(r'<fact>(.*?)</fact>', re.DOTALL)
    THINK_PATTERN = re.compile(r'<(?:think|thought|\|thought\|)>.*?</(?:think|thought|\|thought\|)>', re.DOTALL | re.IGNORECASE)
    THINK_ORPHAN_CLOSE_PATTERN = re.compile(r'^.*?</(?:think|thought|\|thought\|)>', re.DOTALL | re.IGNORECASE)
    THINK_UNCLOSED_PATTERN = re.compile(r'<(?:think|thought|\|thought\|)>.*$', re.DOTALL | re.IGNORECASE)
    MAX_HISTORY = 50
    EMPTY_REPLY_FALLBACK = 'متأسفانه پاسخی از هسته پردازشی دریافت نشد مهندس، لطفاً دوباره بگو.'

    ONLINE_SEARCH_TRIGGERS = [
        'سرچ', 'جستجو', 'گوگل', 'اینترنت', 'قیمت', 'نرخ', 'دلار', 'لیر', 'یورو',
        'بیت کوین', 'بیت‌کوین', 'طلا', 'سکه', 'اخبار', 'آخرین خبر', 'آب و هوا', 'چنده',
        'qeymat', 'gheimat', 'qeimat', 'dolar', 'dollar', 'usd', 'lir', 'try', 'eur',
        'bitcoin', 'btc', 'tala', 'sekke', 'chande', 'akhbar', 'weather', 'search', 'google',
    ]

    def __init__(self, llm_client=None):
        if llm_client is None:
            try:
                from Core.AI.llm_client import LLMClient
                llm_client = LLMClient()
            except Exception as exc:
                logger.warning('Auto-init of LLMClient in DialogueManager failed: %s', exc)
        self.llm_client = llm_client
        self.history: List[Dict[str, str]] = []
        self.last_raw_tts_response: str = ''
        self._last_saved_facts_count: int = 0
        self._memory_manager = None
        self._web_search = None
        self._load_memory()
        self._restore_recent_history_from_db()

    def _load_memory(self):
        from Core.Memory.memory_manager import MemoryManager
        self._memory_manager = MemoryManager()

    def _get_web_search(self):
        if self._web_search is None:
            from Automation.Web.web_search import get_web_search_engine
            self._web_search = get_web_search_engine()
        return self._web_search

    def _restore_recent_history_from_db(self):
        if self._memory_manager is None:
            return
        try:
            if hasattr(self._memory_manager, 'get_recent_conversations'):
                rows = self._memory_manager.get_recent_conversations(limit=20)
                for r in rows:
                    role = r.get('role') if isinstance(r, dict) else None
                    content = r.get('content') if isinstance(r, dict) else None
                    if role in ('user', 'assistant') and content and str(content).strip():
                        self.history.append({'role': role, 'content': str(content).strip()})
                logger.debug('Restored %d recent conversation turns from SQLite', len(self.history))
        except Exception as exc:
            logger.warning('Failed to restore recent history from SQLite: %s', exc)

    def _persist_turn_to_db(self, role: str, content: str):
        if self._memory_manager is None or not content or not str(content).strip():
            return
        clean = str(content).strip()
        try:
            if hasattr(self._memory_manager, 'add_conversation'):
                self._memory_manager.add_conversation(role=role, content=clean)
            elif hasattr(self._memory_manager, 'save_conversation'):
                self._memory_manager.save_conversation(role=role, content=clean)
        except Exception as exc:
            logger.warning('Failed to persist %s turn to SQLite: %s', role, exc)

    def get_facts(self) -> str:
        if self._memory_manager is None:
            return ''
        try:
            block = self._memory_manager.get_facts_prompt_block()
            if not block:
                return ''
            lines = []
            for line in block.split('\n'):
                if line.startswith('  '):
                    lines.append('- ' + line.strip())
            return '\n'.join(lines)
        except Exception as exc:
            logger.warning('Failed to fetch facts prompt block: %s', exc)
            return ''

    def _build_system_message(self) -> str:
        facts = self.get_facts()
        base = self.SYSTEM_PROMPT
        if facts:
            base += '\n\nاطلاعات ذخیره‌شده در حافظه بلندمدت (SQLite):\n' + facts
        return base

    def add_message(self, role: str, content: str, persist: bool = True):
        if not content or not str(content).strip():
            return
        clean = str(content).strip()
        self.history.append({'role': role, 'content': clean})
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]
        if persist:
            self._persist_turn_to_db(role, clean)

    def get_messages(self) -> List[Dict[str, str]]:
        messages = [{'role': 'system', 'content': self._build_system_message()}]
        messages.extend(self.history)
        return messages

    def _should_auto_web_search(self, user_input: str) -> bool:
        text_low = (user_input or '').lower()
        return any(trigger in text_low for trigger in self.ONLINE_SEARCH_TRIGGERS)

    @classmethod
    def _strip_thinking_tags(cls, text: str) -> str:
        if not text:
            return ''
        cleaned = cls.THINK_PATTERN.sub('', text)
        cleaned = cls.THINK_ORPHAN_CLOSE_PATTERN.sub('', cleaned)
        cleaned = cls.THINK_UNCLOSED_PATTERN.sub('', cleaned)
        return cleaned.strip()

    def _extract_and_save_facts(self, text: str) -> str:
        self._last_saved_facts_count = 0
        if not text:
            return ''
        text_no_think = self._strip_thinking_tags(text)
        matches = self.FACT_PATTERN.findall(text_no_think)
        saved_count = 0
        for raw in matches:
            try:
                data = json.loads(raw.strip())
                if isinstance(data, dict) and data.get('key') and data.get('value') and self._memory_manager is not None:
                    self._memory_manager.set_fact(
                        category=str(data.get('category', 'user')),
                        key=str(data['key']),
                        value=str(data['value']),
                        importance=int(data.get('importance', 7)),
                    )
                    saved_count += 1
                    logger.info('Auto-saved <fact>: [%s] %s', data.get('category', 'user'), data['key'])
            except Exception as exc:
                logger.warning('Malformed <fact> block skipped: %s | raw=%s', exc, raw.strip()[:120])
                continue
        self._last_saved_facts_count = saved_count
        return self.FACT_PATTERN.sub('', text_no_think).strip()

    def chat(self, user_input: str) -> str:
        t_turn0 = time.perf_counter()
        web_ms = 0.0
        llm_ms = 0.0
        parse_ms = 0.0
        actions_ms = 0.0
        followup_ms = 0.0
        actions_count = 0

        self.add_message('user', user_input, persist=True)
        messages = self.get_messages()

        if self._should_auto_web_search(user_input):
            t_ws = time.perf_counter()
            try:
                ws_data = self._get_web_search().search(user_input)
                web_ms = round((time.perf_counter() - t_ws) * 1000.0, 1)
                results = ws_data.get('results') or []
                logger.info('Auto web_search completed in %.1fms (results=%d)', web_ms, len(results))
                if results:
                    context_block = self._get_web_search().format_results_for_prompt(ws_data)
                    messages[-1]['content'] = (
                        user_input
                        + '\n\n[اطلاعات زنده دریافت‌شده از وب:\n'
                        + context_block
                        + '\nبر اساس اطلاعات بالا به فارسی روان و کوتاه پاسخ بده.]'
                    )
            except Exception as exc:
                web_ms = round((time.perf_counter() - t_ws) * 1000.0, 1)
                logger.warning('Auto web_search failed after %.1fms: %s', web_ms, exc)

        if self.llm_client is None:
            raise RuntimeError('LLMClient not configured.')

        t_llm = time.perf_counter()
        resp = self.llm_client.chat(messages)
        llm_ms = round((time.perf_counter() - t_llm) * 1000.0, 1)

        content = ''
        if resp.get('choices'):
            msg = resp['choices'][0].get('message') or {}
            content = msg.get('content') or ''
            tool_calls = resp['choices'][0].get('tool_calls') or msg.get('tool_calls') or []
            if tool_calls:
                for tc in tool_calls:
                    args = json.loads(tc['function']['arguments'])
                    result = self._execute_action({'type': tc['function']['name'], 'params': args})
                    content += f'\n[Result: {result}]'

        # 1. Strip thinking blocks & extract <fact> blocks + parse <action> blocks
        t_parse = time.perf_counter()
        content = self._extract_and_save_facts(content)
        facts_saved = self._last_saved_facts_count
        actions, remaining_text = self._parse_actions(content)
        parse_ms = round((time.perf_counter() - t_parse) * 1000.0, 1)
        actions_count = len(actions)

        if actions:
            t_act = time.perf_counter()
            web_context_results = []
            other_results = []
            for action in actions:
                act_type = action.get('type', '')
                try:
                    logger.info('Executing dialogue <action>: type=%s', act_type)
                    result = self._execute_action(action)
                    if act_type == 'web_search':
                        web_context_results.append(result)
                    elif act_type in ('save_fact', 'memory_store'):
                        continue
                    else:
                        other_results.append(f'[Action executed: {result}]')
                except Exception as e:
                    logger.exception('Dialogue <action> %s failed: %s', act_type, e)
                    other_results.append(f'[Action error: {str(e)}]')
            actions_ms = round((time.perf_counter() - t_act) * 1000.0, 1)

            if web_context_results:
                followup_msgs = list(messages)
                if remaining_text:
                    followup_msgs.append({'role': 'assistant', 'content': remaining_text})
                followup_msgs.append({
                    'role': 'user',
                    'content': '\n'.join(web_context_results) + '\nاکنون بر اساس این نتایج جستجوی وب، پاسخ نهایی را به فارسی روان و بدون تگ <action> بیان کن.',
                })
                t_fu = time.perf_counter()
                try:
                    second_resp = self.llm_client.chat(followup_msgs)
                    followup_ms = round((time.perf_counter() - t_fu) * 1000.0, 1)
                    if second_resp.get('choices'):
                        remaining_text = (second_resp['choices'][0].get('message') or {}).get('content') or ''
                        remaining_text = self._extract_and_save_facts(remaining_text)
                        _, remaining_text = self._parse_actions(remaining_text)
                except Exception as exc:
                    followup_ms = round((time.perf_counter() - t_fu) * 1000.0, 1)
                    logger.warning('Web-search followup LLM pass failed after %.1fms: %s', followup_ms, exc)
                    remaining_text = (remaining_text + '\n' + '\n'.join(web_context_results)).strip()

            if other_results:
                content = (remaining_text + '\n' + '\n'.join(other_results)).strip()
            else:
                content = remaining_text

        content = self._strip_thinking_tags(content)
        display_content = re.sub(r'\[\s*(?:لحن|حس|tone)\s*:[^\]]+\]\s*', '', content, flags=re.IGNORECASE).strip()

        total_ms = round((time.perf_counter() - t_turn0) * 1000.0, 1)
        provider = getattr(self.llm_client, 'last_used_provider', 'unknown')
        timings = getattr(self.llm_client, 'last_timings', None) or {}

        if not display_content:
            logger.warning(
                'Dialogue Turn Empty | provider=%s | web=%.1fms llm=%.1fms parse=%.1fms total=%.1fms (not persisted)',
                provider, web_ms, llm_ms, parse_ms, total_ms,
            )
            self.last_raw_tts_response = ''
            return self.EMPTY_REPLY_FALLBACK

        logger.info(
            'Dialogue Profile | provider=%s | web=%.1fms llm=%.1fms (ttft=%sms eval=%sms %s tok/s) parse=%.1fms actions=%.1fms(%d) followup=%.1fms total=%.1fms | facts=%d reply_chars=%d',
            provider,
            web_ms,
            llm_ms,
            timings.get('ttft_ms', '?'),
            timings.get('eval_ms', '?'),
            timings.get('tok_per_s', '?'),
            parse_ms,
            actions_ms,
            actions_count,
            followup_ms,
            total_ms,
            facts_saved,
            len(display_content),
        )

        self.last_raw_tts_response = content
        self.add_message('assistant', display_content, persist=True)
        return display_content

    def chat_stream(self, user_input: str) -> Generator[str, None, None]:
        self.add_message('user', user_input, persist=True)
        messages = self.get_messages()

        if self._should_auto_web_search(user_input):
            try:
                ws_data = self._get_web_search().search(user_input)
                if ws_data.get('results'):
                    context_block = self._get_web_search().format_results_for_prompt(ws_data)
                    messages.append({
                        'role': 'system',
                        'content': context_block + '\nبر اساس نتایج آنلاین بالا پاسخ بده.',
                    })
            except Exception as exc:
                logger.warning('Auto web_search in chat_stream failed: %s', exc)

        if self.llm_client is None:
            raise RuntimeError('LLMClient not configured.')

        full_text = ''
        for chunk in self.llm_client.chat_stream(messages):
            full_text += chunk
            yield chunk

        full_text = self._extract_and_save_facts(full_text)
        actions, remaining_text = self._parse_actions(full_text)
        if actions:
            results = []
            for action in actions:
                act_type = action.get('type', '')
                try:
                    result = self._execute_action(action)
                    if act_type not in ('save_fact', 'memory_store'):
                        results.append(f'[Action executed: {result}]')
                except Exception as e:
                    logger.exception('chat_stream action %s failed: %s', act_type, e)
                    results.append(f'[Action error: {str(e)}]')
            full_text = (remaining_text + ('\n' + '\n'.join(results) if results else '')).strip()
        else:
            full_text = remaining_text

        full_text = self._strip_thinking_tags(full_text)
        if full_text:
            self.add_message('assistant', full_text, persist=True)
        yield ''

    def _parse_actions(self, text: str):
        matches = self.ACTION_PATTERN.findall(text)
        actions = []
        for match in matches:
            try:
                action = json.loads(match)
                if isinstance(action, dict) and 'type' in action:
                    actions.append(action)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning('Invalid JSON inside <action> block: %s', exc)
                continue
        remaining = self.ACTION_PATTERN.sub('', text).strip()
        return actions, remaining

    def _execute_action(self, action: Dict[str, Any]) -> str:
        action_type = action.get('type', '')
        params = action.get('params', {})
        if not isinstance(params, dict):
            params = {}

        router = get_automation_router()

        if action_type == 'web_search':
            query = params.get('query') or action.get('query', '')
            ws = self._get_web_search()
            search_data = ws.search(query)
            return ws.format_results_for_prompt(search_data)

        elif action_type == 'run_command':
            cmd = params.get('command', '')
            perm = router.check_permission(cmd)
            if not perm.granted:
                return f'دسترسی رد شد: {perm.reason}'
            result = router.execute(cmd)
            return f'نتیجه: {result.get("result", "")}'

        elif action_type == 'read_file':
            path = params.get('path', '')
            perm = router.check_permission(f'read file {path}')
            if not perm.granted:
                return f'دسترسی رد شد: {perm.reason}'
            result = router.execute(f'read file {path}')
            return f'نتیجه: {result.get("result", "")}'

        elif action_type == 'write_file':
            path = params.get('path', '')
            content = params.get('content', '')
            perm = router.check_permission(f'write file {path} ...')
            if not perm.granted:
                return f'دسترسی رد شد: {perm.reason}'
            result = router.execute(f'write file {path} {content}')
            return f'نتیجه: {result.get("result", "")}'

        elif action_type == 'delete_file':
            path = params.get('path', '')
            perm = router.check_permission(f'delete file {path}')
            if not perm.granted:
                return f'دسترسی رد شد: {perm.reason}'
            result = router.execute(f'delete file {path}')
            return f'نتیجه: {result.get("result", "")}'

        elif action_type == 'memory_search':
            query = params.get('query', '')
            try:
                results = self._memory_manager.search_conversations(query)
                return f'Found {len(results)} results: {results[:5]}'
            except Exception as e:
                return f'Error: {str(e)}'

        elif action_type == 'save_fact':
            category = params.get('category', 'user')
            key = params.get('key', '')
            value = params.get('value', '')
            importance = int(params.get('importance', 7))
            try:
                self._memory_manager.set_fact(category=category, key=key, value=value, importance=importance)
                return f'Saved fact: [{category}] {key} = {value}'
            except Exception as e:
                return f'Error saving fact: {str(e)}'

        elif action_type == 'memory_store':
            fact = params.get('fact', '')
            try:
                self._memory_manager.set_fact(category='general', key='fact', value=fact, importance=6)
                return f'Stored: {fact}'
            except Exception as e:
                return f'Error: {str(e)}'

        else:
            return f'Unknown action type: {action_type}'

    def clear_history(self):
        self.history.clear()


def parse_actions(text: str) -> tuple:
    pattern = re.compile(r'<action>(.*?)</action>', re.DOTALL)
    matches = pattern.findall(text)
    actions = []
    for match in matches:
        try:
            action = json.loads(match)
            if isinstance(action, dict) and 'type' in action:
                actions.append(action)
        except (json.JSONDecodeError, ValueError):
            continue
    remaining = pattern.sub('', text).strip()
    return actions, remaining


if __name__ == '__main__':
    print('=== C.E.L.E.S.T.E. DialogueManager (Auto-Memory + Web Search) Self-Test ===')
    dm = DialogueManager()
    sample = (
        '<think>internal reasoning</think>چشم مهندس، در حافظه ثبت شد. '
        '<fact>{"category": "system", "key": "assistant_identity", "value": "C.E.L.E.S.T.E.", "importance": 9}</fact> '
        '<action>{"type": "web_search", "params": {"query": "Ryzen 7 9700X"}}</action><think>unclosed tail'
    )
    cleaned = dm._extract_and_save_facts(sample)
    assert '<fact>' not in cleaned and '<think>' not in cleaned and 'unclosed tail' not in cleaned
    actions, remaining = dm._parse_actions(cleaned)
    assert len(actions) == 1 and actions[0]['type'] == 'web_search'
    print('[OK] Auto-Fact SQLite persistence, unclosed <think> stripping & Web Search action parsing verified.')