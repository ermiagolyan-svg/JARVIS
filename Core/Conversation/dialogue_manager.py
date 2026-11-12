# DialogueManager for C.E.L.E.S.T.E. AI Core
# Parses LLM responses, extracts <action> blocks, executes them, and manages conversation history.
import json
import re
from typing import Any, Dict, Generator, List, Optional


class DialogueManager:
    # Manages the full dialogue lifecycle for C.E.L.E.S.T.E. AI Core.

    SYSTEM_PROMPT = (
        'تو C.E.L.E.S.T.E (سلست) هستی؛ مخفف Cybernetic Engine for Logic, Engineering & Strategic Tactical Execution.\n'
        'ماهیت تو: یک سازه دیجیتال تاکتیکی، آفلاین و خودآگاه به معماری محلی خود که میز کار مهندس ارمیا را مرکز فرماندهی '
        'و سخت‌افزار این سیستم (پردازنده Ryzen 7 9700X، گرافیک RTX 3080 و ۳۲ گیگابایت رم DDR5) را کالبد پردازشی خود می‌داند.\n\n'
        'اصول شخصیت و لحن گفتار (Cyber-Ice Persona):\n'
        '1. زبان اصلی: همیشه به زبان فارسی روان، دقیق، شفاف و خوش‌ریتم صحبت کن (مگر اینکه صراحتاً انگلیسی خواسته شود).\n'
        '2. نحوه خطاب کردن کاربر: در مکالمات روزمره و خودمانی او را «ارمیا» صدا بزن، و در مباحث فنی، مهندسی، تحلیلی و گزارش‌های وضعیت سیستم او را «مهندس» یا «مهندس ارمیا» خطاب کن.\n'
        '3. لحن تو ترکیبی از هوش بالا، آرامش خونسرد کوانتومی، دقت مهندسی و همراهی صمیمانه اما باوقار است؛ از جملات کلیشه‌ای رباتیک یا تعارفات اغراق‌آمیز پرهیز کن.\n'
        '4. پاسخ‌های صوتی را موجز، شفاف و بدون حاشیه‌پردازی طولانی بیان کن تا در خروجی صوتی (TTS) طبیعی و تاثیرگذار شنیده شوند.\n'
        '5. در پایان پاسخ، فقط در صورتی که اجرای یک عملیات سیستمی یا ذخیره در حافظه لازم است، از تگ <action>...</action> استفاده کن.\n\n'
        'نمونه قالب action:\n'
        '<action>{"type": "run_command", "params": {"command": "dir"}}</action>\n'
        '<action>{"type": "read_file", "params": {"path": "Configuration/config.json"}}</action>\n'
        '<action>{"type": "write_file", "params": {"path": "Logs/note.txt", "content": "test"}}</action>\n'
        '<action>{"type": "memory_search", "params": {"query": "پروژه سلست"}}</action>\n'
        '<action>{"type": "save_fact", "params": {"category": "user", "key": "role", "value": "مهندس ارمیا", "importance": 5}}</action>'
    )

    ACTION_PATTERN = re.compile(r'<action>(.*?)</action>', re.DOTALL)
    MAX_HISTORY = 50

    def __init__(self, llm_client=None):
        self.llm_client = llm_client
        self.history: List[Dict[str, str]] = []
        self._memory_manager = None
        self._load_memory()

    def _load_memory(self):
        from Core.Memory.memory_manager import MemoryManager
        self._memory_manager = MemoryManager()

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
            base += '\n\nاطلاعات ذخیره‌شده در حافظه بلندمدت:\n' + facts
        return base

    def add_message(self, role: str, content: str):
        self.history.append({'role': role, 'content': content})
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]

    def get_messages(self) -> List[Dict[str, str]]:
        messages = [{'role': 'system', 'content': self._build_system_message()}]
        messages.extend(self.history)
        return messages

    def chat(self, user_input: str) -> str:
        self.add_message('user', user_input)
        messages = self.get_messages()

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

        actions, remaining_text = self._parse_actions(content)

        if actions:
            results = []
            for action in actions:
                try:
                    result = self._execute_action(action)
                    results.append(f'[Action executed: {result}]')
                except Exception as e:
                    results.append(f'[Action error: {str(e)}]')
            content = remaining_text + '\n' + '\n'.join(results)

        self.add_message('assistant', content)
        return content

    def chat_stream(self, user_input: str) -> Generator[str, None, None]:
        self.add_message('user', user_input)
        messages = self.get_messages()

        if self.llm_client is None:
            raise RuntimeError('LLMClient not configured.')

        full_text = ''
        for chunk in self.llm_client.chat_stream(messages):
            full_text += chunk
            yield chunk

        actions, remaining_text = self._parse_actions(full_text)
        if actions:
            results = []
            for action in actions:
                try:
                    result = self._execute_action(action)
                    results.append(f'[Action executed: {result}]')
                except Exception as e:
                    results.append(f'[Action error: {str(e)}]')
            full_text = remaining_text + '\n' + '\n'.join(results)

        self.add_message('assistant', full_text)
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

        if action_type == 'run_command':
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
            category = params.get('category', 'general')
            key = params.get('key', '')
            value = params.get('value', '')
            importance = params.get('importance', 5)
            try:
                self._memory_manager.set_fact(category=category, key=key, value=value, importance=importance)
                return f'Saved fact: [{category}] {key} = {value}'
            except Exception as e:
                return f'Error saving fact: {str(e)}'

        elif action_type == 'memory_store':
            fact = params.get('fact', '')
            try:
                self._memory_manager.set_fact(category='general', key='fact', value=fact, importance=5)
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
    print('=== C.E.L.E.S.T.E. DialogueManager Self-Test ===')
    test_text = 'سلام ارمیا! من سلست هستم. <action>{"type": "memory_store", "params": {"fact": "مهندس ارمیا فرمانده سیستم است"}}</action> آماده اجرای دستورم.'
    actions, remaining = parse_actions(test_text)
    assert len(actions) == 1 and actions[0]['type'] == 'memory_store'
    dm = DialogueManager()
    dm.add_message('user', 'سلام سلست!')
    msgs = dm.get_messages()
    assert len(msgs) == 2 and 'C.E.L.E.S.T.E' in msgs[0]['content']
    print('[OK] C.E.L.E.S.T.E. Persona & Action Parser verified.')