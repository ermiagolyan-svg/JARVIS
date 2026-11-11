"""Memory manager for JARVIS — wraps DatabaseManager and ConfigManager."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')  # project root
sys.path.append('D:\\AI\\JARVIS')  # project root (one more level up)

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class MemoryManager:
    """High-level memory operations backed by SQLite (DatabaseManager) and config (ConfigManager)."""

    def __init__(self):
        from Core.Memory.db_manager import get_db as _get_db
        from Core.Config.config_loader import get_config as _get_config

        self.db = _get_db()
        self.cfg = _get_config()
        self.max_context_messages: int = 20
        self.retention_days: int = 365
        self._settings_loaded = False

    def load_settings(self):
        """Load retention / limit settings from ConfigManager."""
        if not hasattr(self, '_settings_loaded'):
            section = self.cfg.get_section("memory") or {}
            self.max_context_messages = int(section.get("max_context_messages", 20))
            self.retention_days = int(section.get("retention_days", 365))
            self._settings_loaded = True

    def add_message(self, session_id: str, role: str, content: str, language: str = 'fa') -> None:
        """Insert a conversation message row."""
        self.load_settings() if not hasattr(self, '_settings_loaded') else None
        self.db.execute_query(
            "INSERT INTO conversations (session_id, role, content, language) VALUES (?, ?, ?, ?)",
            (session_id, role.lower(), content, language),
        )

    def get_recent_context(self, limit: int = 20) -> List[sqlite3.Row]:
        """Return recent messages ordered by timestamp."""
        self.load_settings() if not hasattr(self, '_settings_loaded') else None
        return self.db.fetch_all(
            "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,),
        )

    def search_conversations(self, query: str) -> List[sqlite3.Row]:
        """Search messages by keyword using LIKE."""
        term = f'%{query}%'
        return self.db.fetch_all(
            "SELECT * FROM conversations WHERE content LIKE ? ORDER BY id DESC", (term,),
        )

    def set_fact(self, category: str, key: str, value: str, importance: int = 5) -> None:
        """Upsert a user fact."""
        self.db.execute_query(
            "INSERT OR REPLACE INTO user_facts (category, fact_key, fact_value, importance) VALUES (?, ?, ?, ?)",
            (category.lower(), key.lower(), value, importance),
        )

    def get_facts_prompt_block(self) -> str:
        """Return a formatted block of all facts for prompting."""
        rows = self.db.fetch_all("SELECT * FROM user_facts ORDER BY importance DESC")
        if not rows:
            return ""
        lines = ["[FACTS]"]
        for row in rows:
            cat = dict(row).get("category", "")
            key_ = dict(row).get("fact_key", "")
            val = dict(row).get("fact_value", "")
            imp = int(dict(row).get("importance", 0))
            lines.append(f'  {cat}:{key_}={val} (importance={imp})')
        return "\n".join(lines)

    def delete_fact(self, fact_id: Optional[int] = None, fact_key: Optional[str] = None) -> bool:
        """Delete a fact by id OR fact_key using self.db.execute_query."""
        if fact_id is not None:
            self.db.execute_query("DELETE FROM user_facts WHERE id = ?", (fact_id,))
            return True
        if fact_key is not None:
            self.db.execute_query("DELETE FROM user_facts WHERE fact_key = ?", (fact_key.lower(),))
            return True
        return False

    def clear_history(self, session_id: Optional[str] = None) -> int:
        """Clear conversation history; optionally for a single session."""
        if session_id is not None:
            self.db.execute_query("DELETE FROM conversations WHERE session_id = ?", (session_id,))
        else:
            self.db.execute_query("DELETE FROM conversations", ())
        return 0

    def cleanup_old_memories(self, days: int = 365) -> int:
        """Remove messages older than `days` days."""
        cutoff = datetime.now() - timedelta(days=days)
        ts = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute_query("DELETE FROM conversations WHERE timestamp < ?", (ts,))
        return 0

    def set_preference(self, key: str, value: Any) -> None:
        """Store a preference."""
        self.db.execute_query(
            "INSERT OR REPLACE INTO preferences (key, value) VALUES (?, ?)",
            (key.lower(), str(value)),
        )

    def get_preference(self, key: str, default: Optional[str] = None) -> Any:
        """Retrieve a preference by key."""
        row = self.db.fetch_one("SELECT * FROM preferences WHERE key = ?", (key.lower(),))
        if row is None:
            return default
        return dict(row).get("value", default)


if __name__ == '__main__':
    mm = MemoryManager()

    print("--- add_message ---")
    mm.add_message('s1', 'user', 'سلام جارویس')
    mm.add_message('s2', 'assistant', 'Hello JARVIS! How can I help?')
    rows = mm.get_recent_context(5)
    for r in rows:
        print(dict(r))

    print("\n--- search ---")
    hits = mm.search_conversations('سلام')
    print(f'Found {len(hits)} results')

    print("\n--- facts ---")
    mm.set_fact('person', 'name', 'Ali', 8)
    mm.set_fact('preference', 'color', 'red', 3)
    block = mm.get_facts_prompt_block()
    print(block if block else '(empty)')

    print("\n--- delete fact ---")
    mm.delete_fact(fact_key='name')
    print(mm.get_facts_prompt_block())

    print("\n--- preferences ---")
    mm.set_preference('theme', 'dark')
    val = mm.get_preference('theme')
    print(f'theme={val}')
    unknown = mm.get_preference('missing', 'fallback')
    print(f'missing={unknown}')

    print("\n--- cleanup & clear ---")
    mm.cleanup_old_memories(30)
    print('cleanup done')
    mm.clear_history()
    print('history cleared')
