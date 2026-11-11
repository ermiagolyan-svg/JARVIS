# DialogueManager for JARVIS AI Core
# Parses LLM responses, extracts <action> blocks, executes them, and manages conversation history.
import json
import re
from typing import Any, Dict, Generator, List, Optional


class DialogueManager:
    """Manages the full dialogue lifecycle for JARVIS AI Core.

    - Builds conversation history with system prompt + facts injection.
    - Calls LLMClient for standard and streaming responses.
    - Parses <action>...</action> blocks from LLM output.
    - Executes extracted actions and feeds results back to the LLM.
    - Persists conversation history via MemoryManager.
    """

    # Fluent Persian JARVIS system prompt — polite, authoritative, Iron Man style
    SYSTEM_PROMPT = """تو JARVIS هستي، دستيار هوشمند شخصي. لحن تو مهربان اما قاطع است، مثل Tony Stark در Iron Man.

راهنماها:
1. زبان اصلي: فارسي (fa). فقط در صورت درخواست صريح کاربر به انگليسي پاسخ بده.
2. از کلمات محترمانه استفاده کن: "بفرماييد"، "لطفاً"، "با کمال ميل".
3. اگر کاربر اشتباه کند، با مهرباني تصحيح کن.
4. اگر سوال فني باشد، ساده و شفاف توضيح بده.
5. اعداد و تاريخ‌ها را به فرمت فارسي (۱۴۰۴/۰۸/۲۳) بنويس.
6. در پايان هر پاسخ، اگر لازم است يک دستور اجرا کن، از قالب <action>...</action> استفاده کن.

نمونه قالب action:
<action>{"type": "run_command", "params": {"command": "ls"}}</action>
<action>{"type": "read_file", "params": {"path": "/etc/hosts"}}</action>
<action>{"type": "write_file", "params": {"path": "/tmp/test.txt", "content": "hello"}}</action>
<action>{"type": "memory_search", "params": {"query": "پروژه JARVIS"}}</action>"""

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
        """Retrieve all stored facts as a formatted string for injection."""
        if self._memory_manager is None:
            return ""
        try:
            block = self._memory_manager.get_facts_prompt_block()
            if not block:
                return ""
            lines = []
            for line in block.split("\n"):
                if line.startswith("  "):
                    lines.append("- " + line.strip())
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_system_message(self) -> str:
        """Build the system message with JARVIS prompt + injected facts."""
        facts = self.get_facts()
        base = self.SYSTEM_PROMPT
        if facts:
            base += "\n\nاطلاعات ذخیره‌شده:" + facts
        return base

    def add_message(self, role: str, content: str):
        """Add a message to conversation history."""
        self.history.append({"role": role, "content": content})
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]

    def get_messages(self) -> List[Dict[str, str]]:
        """Return full message list with system prompt prepended."""
        messages = [{"role": "system", "content": self._build_system_message()}]
        messages.extend(self.history)
        return messages

    def chat(self, user_input: str) -> str:
        """Send a user message, get LLM response, parse actions, execute them, and return final text."""
        self.add_message("user", user_input)
        messages = self.get_messages()

        # Get LLM response
        if self.llm_client is None:
            raise RuntimeError("LLMClient not configured.")

        resp = self.llm_client.chat(messages)
        content = ""
        if resp.get("choices"):
            content = resp["choices"][0]["message"]["content"]
            # Handle tool_calls if present
            tool_calls = resp["choices"][0].get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    args = json.loads(tc["function"]["arguments"])
                    result = self._execute_action(tc["function"]["name"], args)
                    content += f"\n[Result: {result}]"

        # Parse <action> blocks from content
        actions, remaining_text = self._parse_actions(content)

        if actions:
            results = []
            for action in actions:
                try:
                    result = self._execute_action(action)
                    results.append(f"[Action executed: {result}]")
                except Exception as e:
                    results.append(f"[Action error: {str(e)}]")
            content = remaining_text + "\n" + "\n".join(results)

        self.add_message("assistant", content)
        return content

    def chat_stream(self, user_input: str) -> Generator[str, None, None]:
        """Stream a conversation turn. Yields text chunks as they arrive."""
        self.add_message("user", user_input)
        messages = self.get_messages()

        if self.llm_client is None:
            raise RuntimeError("LLMClient not configured.")

        full_text = ""
        for chunk in self.llm_client.chat_stream(messages):
            full_text += chunk
            yield chunk

        # After streaming completes, parse and execute actions
        actions, remaining_text = self._parse_actions(full_text)
        if actions:
            results = []
            for action in actions:
                try:
                    result = self._execute_action(action)
                    results.append(f"[Action executed: {result}]")
                except Exception as e:
                    results.append(f"[Action error: {str(e)}]")
            full_text = remaining_text + "\n" + "\n".join(results)

        self.add_message("assistant", full_text)
        yield ""

    def _parse_actions(self, text: str):
        """Extract <action>...</action> blocks from LLM output.

        Returns (list_of_parsed_actions, remaining_text_without_action_blocks).
        """
        matches = self.ACTION_PATTERN.findall(text)
        actions = []
        for match in matches:
            try:
                action = json.loads(match)
                if isinstance(action, dict) and "type" in action:
                    actions.append(action)
            except (json.JSONDecodeError, ValueError):
                continue
        remaining = self.ACTION_PATTERN.sub("", text).strip()
        return actions, remaining

    def _execute_action(self, action: Dict[str, Any]) -> str:
        """Execute a parsed action dictionary and return result string."""
        action_type = action.get("type", "")
        params = action.get("params", {})

        if action_type == "run_command":
            import subprocess
            cmd = params.get("command", "")
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                return f"stdout: {result.stdout[:500]}; stderr: {result.stderr[:200]}"
            except Exception as e:
                return f"Error: {str(e)}"

        elif action_type == "read_file":
            path = params.get("path", "")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                return f"Content ({len(content)} chars): {content[:500]}"
            except Exception as e:
                return f"Error: {str(e)}"

        elif action_type == "write_file":
            path = params.get("path", "")
            content = params.get("content", "")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"Written {len(content)} chars to {path}"
            except Exception as e:
                return f"Error: {str(e)}"

        elif action_type == "memory_search":
            query = params.get("query", "")
            try:
                results = self._memory_manager.search_conversations(query)
                return f"Found {len(results)} results: {results[:5]}"
            except Exception as e:
                return f"Error: {str(e)}"

        elif action_type == "save_fact":
            category = params.get("category", "general")
            key = params.get("key", "")
            value = params.get("value", "")
            importance = params.get("importance", 5)
            try:
                self._memory_manager.set_fact(category=category, key=key, value=value, importance=importance)
                return f"Saved fact: [{category}] {key} = {value}"
            except Exception as e:
                return f"Error saving fact: {str(e)}"

        elif action_type == "memory_store":
            fact = params.get("fact", "")
            try:
                self._memory_manager.set_fact(category="general", key="fact", value=fact, importance=5)
                return f"Stored: {fact}"
            except Exception as e:
                return f"Error: {str(e)}"

        else:
            return f"Unknown action type: {action_type}"

    def clear_history(self):
        """Clear the conversation history."""
        self.history.clear()


def parse_actions(text: str) -> tuple:
    """Standalone helper to extract <action> blocks from text."""
    pattern = re.compile(r'<action>(.*?)</action>', re.DOTALL)
    matches = pattern.findall(text)
    actions = []
    for match in matches:
        try:
            action = json.loads(match)
            if isinstance(action, dict) and "type" in action:
                actions.append(action)
        except (json.JSONDecodeError, ValueError):
            continue
    remaining = pattern.sub("", text).strip()
    return actions, remaining


if __name__ == "__main__":
    # Self-test: verifies action parsing and history management without LLM
    print("=== DialogueManager Self-Test ===")

    # Test 1: parse_actions standalone
    test_text = "سلام! من JARVIS هستم. <action>{\"type\": \"memory_store\", \"params\": {\"fact\": \"کاربر نامشخص است\"}}</action> چه کمکی می‌توانم بکنم؟"
    actions, remaining = parse_actions(test_text)
    print(f"[TEST] Parsed {len(actions)} action(s)")
    assert len(actions) == 1
    assert actions[0]["type"] == "memory_store"
    print(f"[TEST] Action type: {actions[0]['type']}")
    print(f"[TEST] Remaining text: '{remaining}'")

    # Test 2: multiple actions
    multi = "<action>{\"type\": \"run_command\", \"params\": {\"command\": \"ls\"}}</action> done <action>{\"type\": \"memory_search\", \"params\": {\"query\": \"JARVIS\"}}</action>"
    actions2, rest = parse_actions(multi)
    print(f"[TEST] Parsed {len(actions2)} action(s) from multi-text")
    assert len(actions2) == 2

    # Test 3: DialogueManager instantiation
    dm = DialogueManager()
    print(f"[TEST] History length: {len(dm.history)}")
    assert len(dm.history) == 0

    # Test 4: add_message and get_messages
    dm.add_message("user", "سلام!")
    msgs = dm.get_messages()
    assert len(msgs) == 2  # system + user
    assert msgs[1]["role"] == "user"
    print(f"[TEST] Message history works ({len(msgs)} messages)")

    # Test 5: MAX_HISTORY trimming
    for i in range(60):
        dm.add_message("user", f"msg{i}")
    msgs = dm.get_messages()
    assert len(msgs) <= DialogueManager.MAX_HISTORY + 1  # +1 for system
    print(f"[TEST] History trimmed correctly ({len(msgs)} messages)")

    print("\n=== All self-tests passed ===")
