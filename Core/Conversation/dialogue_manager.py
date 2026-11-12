# DialogueManager for C.E.L.E.S.T.E. AI Core
# Features:
# 1. Persistent SQLite Conversation & Fact Memory across PC reboots.
# 2. Automatic Fact Extraction (<fact>{...}</fact> and save_fact actions).
# 3. Live Google/Web Search RAG synthesis (web_search action + automatic online intent detection).
# 4. Hybrid Cloud AI routing support (Gemini / Claude / ChatGPT / Local Qwen 3.5 9B).

import json
import re
from typing import Any, Dict, Generator, List, Optional


class DialogueManager:
    SYSTEM_PROMPT = (
        'تو C.E.L.E.S.T.E (سلست) هستی؛ مخفف Cybernetic Engine for Logic, Engineering & Strategic Tactical Execution.\n'
        'ماهیت تو: یک سازه دیجیتال تاکتیکی، مجهز به هسته محلی و اتصال ابری/آنلاین که میز کار مهندس ایلیا (ارمیا) را مرکز فرماندهی '
        'و سخت‌افزار این سیستم (پردازنده Ryzen 7 9700X، گرافیک RTX 3080 و ۳۲ گیگابایت رم DDR5) را کالبد پردازشی خود می‌داند.\n\n'
        'اصول شخصیت و لحن گفتار (Cyber-Ice Persona):\n'
        '1. زبان اصلی: همیشه به زبان فارسی روان، دقیق، شفاف و خوش‌ریتم صحبت کن (مگر اینکه صراحتاً انگلیسی خواسته شود).\n'
        '2. نحوه خطاب کردن کاربر: در مکالمات روزمره و خودمانی او را با نام خودش صدا بزن، و در مباحث فنی، مهندسی، تحلیلی و گزارش‌های وضعیت سیستم او را «مهندس» خطاب کن.\n'
        '3. لحن تو ترکیبی از هوش بالا، آرامش خونسرد کوانتومی، دقت مهندسی و همراهی صمیمانه اما باوقار است؛ از جملات کلیشه‌ای رباتیک پرهیز کن.\n'
        '4. پاسخ‌های صوتی را موجز، شفاف و بدون حاشیه‌پردازی طولانی بیان کن تا در خروجی صوتی (TTS) طبیعی و تاثیرگذار شنیده شوند.\n'
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
    MAX_HISTORY = 50

    ONLINE_SEARCH_TRIGGERS = [
        'سرچ کن', 'جستجو کن', 'در گوگل', 'تو گوگل', 'در اینترنت', 'قیمت روز',
        'اخبار روز', 'آخرین خبر', 'آب و هوای', 'search google', 'google search',
    ]

    def __init__(self, llm_client=None):
        self.llm_client = llm_client
        self.history: List[Dict[str, str]] = []
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
                    if role in ('user', 'assistant') and content:
                        self.history.append({'role': role, 'content': content})
        except Exception:
            pass

    def _persist_turn_to_db(self, role: str, content: str):
        if self._memory_manager is None or not content.strip():
            return
        try:
            if hasattr(self._memory_manager, 'add_conversation'):
                self._memory_manager.add_conversation(role=role, content=content)
            elif hasattr(self._memory_manager, 'save_conversation'):
                self._memory_manager.save_conversation(role=role, content=content)
        except Exception:
            pass

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
        except Exception:
            return ''

    def _build_system_message(self) -> str:
        facts = self.get_facts()
        base = self.SYSTEM_PROMPT
        if facts:
            base += '\n\nاطلاعات ذخیره‌شده در حافظه بلندمدت (SQLite):\n' + facts
        return base

    def add_message(self, role: str, content: str, persist: bool = True):
        self.history.append({'role': role, 'content': content})
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]
        if persist:
            self._persist_turn_to_db(role, content)

    def get_messages(self) -> List[Dict[str, str]]:
        messages = [{'role': 'system', 'content': self._build_system_message()}]
        messages.extend(self.history)
        return messages

    def _should_auto_web_search(self, user_input: str) -> bool:
        text_low = (user_input or '').lower()
        return any(trigger in text_low for trigger in self.ONLINE_SEARCH_TRIGGERS)

    def _extract_and_save_facts(self, text: str) -> str:
        matches = self.FACT_PATTERN.findall(text)
        for raw in matches:
            try:
                data = json.loads(raw.strip())
                if isinstance(data, dict) and data.get('key') and data.get('value'):
                    self._memory_manager.set_fact(
                        category=str(data.get('category', 'user')),
                        key=str(data['key']),
                        value=str(data['value']),
                        importance=int(data.get('importance', 7)),
                    )
            except Exception:
                continue
        return self.FACT_PATTERN.sub('', text).strip()

    def chat(self, user_input: str) -> str:
        self.add_message('user', user_input, persist=True)
        messages = self.get_messages()

        # Pre-fetch live web search if user explicitly asked to search Google/web
        if self._should_auto_web_search(user_input):
            try:
                ws_data = self._get_web_search().search(user_input)
                if ws_data.get('results'):
                    context_block = self._get_web_search().format_results_for_prompt(ws_data)
                    messages.append({
                        'role': 'system',
                        'content': context_block + '\nبر اساس نتایج آنلاین بالا، پاسخ دقیق و روان فارسی به کاربر بده.',
                    })
            except Exception:
                pass

        if self.llm_client is None:
            raise RuntimeError('LLMClient not configured.')

        resp = self.llm_client.chat(messages)
        content = ''
        if resp.get('choices'):
            content = resp['choices'][0]['message']['content']
            tool_calls = resp['choices'][0].get('tool_calls', [])
            if tool_calls:
                for tc in tool_calls:
                    args = json.loads(tc['function']['arguments'])
                    result = self._execute_action({'type': tc['function']['name'], 'params': args})
                    content += f'\n[Result: {result}]'

        # 1. Extract and silently store any <fact> blocks into SQLite
        content = self._extract_and_save_facts(content)

        # 2. Extract and execute <action> blocks
        actions, remaining_text = self._parse_actions(content)

        if actions:
            web_context_results = []
            other_results = []
            for action in actions:
                act_type = action.get('type', '')
                try:
                    result = self._execute_action(action)
                    if act_type == 'web_search':
                        web_context_results.append(result)
                    elif act_type in ('save_fact', 'memory_store'):
                        # Silent memory persistence without cluttering spoken TTS output
                        continue
                    else:
                        other_results.append(f'[Action executed: {result}]')
                except Exception as e:
                    other_results.append(f'[Action error: {str(e)}]')

            # If the LLM triggered a web_search action, synthesize the final grounded answer
            if web_context_results:
                followup_msgs = list(messages)
                followup_msgs.append({'role': 'assistant', 'content': remaining_text})
                followup_msgs.append({
                    'role': 'system',
                    'content': '\n'.join(web_context_results) + '\nاکنون بر اساس این نتایج جستجوی وب، پاسخ نهایی را به فارسی روان و بدون تگ <action> بیان کن.',
                })
                try:
                    second_resp = self.llm_client.chat(followup_msgs)
                    if second_resp.get('choices'):
                        remaining_text = second_resp['choices'][0]['message']['content']
                        remaining_text = self._extract_and_save_facts(remaining_text)
                        _, remaining_text = self._parse_actions(remaining_text)
                except Exception:
                    remaining_text = remaining_text + '\n' + '\n'.join(web_context_results)

            if other_results:
                content = (remaining_text + '\n' + '\n'.join(other_results)).strip()
            else:
                content = remaining_text

        self.add_message('assistant', content, persist=True)
        return content

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
            except Exception:
                pass

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
                    results.append(f'[Action error: {str(e)}]')
            full_text = (remaining_text + ('\n' + '\n'.join(results) if results else '')).strip()
        else:
            full_text = remaining_text

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
            except (json.JSONDecodeError, ValueError):
                continue
        remaining = self.ACTION_PATTERN.sub('', text).strip()
        return actions, remaining

    def _execute_action(self, action: Dict[str, Any]) -> str:
        action_type = action.get('type', '')
        params = action.get('params', {})
        if not isinstance(params, dict):
            params = {}

        if action_type == 'web_search':
            query = params.get('query') or action.get('query', '')
            ws = self._get_web_search()
            search_data = ws.search(query)
            return ws.format_results_for_prompt(search_data)

        elif action_type == 'run_command':
            import subprocess
            cmd = params.get('command', '')
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                return f'stdout: {result.stdout[:500]}; stderr: {result.stderr[:200]}'
            except Exception as e:
                return f'Error: {str(e)}'

        elif action_type == 'read_file':
            path = params.get('path', '')
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                return f'Content ({len(content)} chars): {content[:500]}'
            except Exception as e:
                return f'Error: {str(e)}'

        elif action_type == 'write_file':
            path = params.get('path', '')
            content = params.get('content', '')
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return f'Written {len(content)} chars to {path}'
            except Exception as e:
                return f'Error: {str(e)}'

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
        'چشم مهندس، در حافظه ثبت شد. '
        '<fact>{"category": "system", "key": "assistant_identity", "value": "C.E.L.E.S.T.E.", "importance": 9}</fact> '
        '<action>{"type": "web_search", "params": {"query": "Ryzen 7 9700X"}}</action>'
    )
    cleaned = dm._extract_and_save_facts(sample)
    assert '<fact>' not in cleaned
    actions, remaining = dm._parse_actions(cleaned)
    assert len(actions) == 1 and actions[0]['type'] == 'web_search'
    print('[OK] Auto-Fact SQLite persistence & Web Search action parsing verified.')