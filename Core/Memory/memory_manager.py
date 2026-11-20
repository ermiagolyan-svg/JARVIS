'''Memory manager for JARVIS — wraps DatabaseManager and ConfigManager.'''
import logging
import os
import sys
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class MemoryManager:
    '''High-level memory operations backed by SQLite (DatabaseManager) and config (ConfigManager).'''

    _instance: Optional['MemoryManager'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> 'MemoryManager':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, '_initialized', False):
            return
        from Core.Memory.db_manager import get_db as _get_db
        from Core.Config.config_loader import get_config as _get_config

        self.db = _get_db()
        self.cfg = _get_config()
        self.max_context_messages: int = 20
        self.retention_days: int = 365
        self._settings_loaded = False
        self._initialized = True

    @classmethod
    def get_memory_manager(cls) -> 'MemoryManager':
        '''Get the singleton MemoryManager instance.'''
        return cls()

    @classmethod
    def get_instance(cls) -> 'MemoryManager':
        '''Alias for get_memory_manager.'''
        return cls()

    def load_settings(self) -> bool:
        '''Load retention / limit settings from ConfigManager.'''
        if not getattr(self, '_settings_loaded', False):
            try:
                section = self.cfg.get_section('memory') or {}
                self.max_context_messages = int(section.get('max_context_messages', 20))
                self.retention_days = int(section.get('retention_days', 365))
                self._settings_loaded = True
                return True
            except Exception as e:
                logger.warning(f"Failed to load memory settings from config: {e}", exc_info=True)
                return False
        return True

    def add_message(self, session_id: str, role: str, content: str, language: str = 'fa') -> None:
        '''Insert a conversation message row.'''
        self.load_settings()
        self.db.execute_query(
            'INSERT INTO conversations (session_id, role, content, language) VALUES (?, ?, ?, ?)',
            (session_id, role.lower(), content, language),
        )

    def get_recent_context(self, limit: int = 20) -> List[sqlite3.Row]:
        '''Return recent messages ordered by timestamp.'''
        self.load_settings()
        return self.db.fetch_all(
            'SELECT * FROM conversations ORDER BY id DESC LIMIT ?', (limit,),
        )

    def get_recent_conversations(self, limit: int = 40) -> List[Dict[str, Any]]:
        '''Return recent conversations as list of dicts.'''
        rows = self.get_recent_context(limit=limit)
        return [dict(r) for r in rows]

    def search_conversations(self, query: str) -> List[sqlite3.Row]:
        '''Search messages by keyword using LIKE.'''
        term = f'%{query}%'
        return self.db.fetch_all(
            'SELECT * FROM conversations WHERE content LIKE ? ORDER BY id DESC', (term,),
        )

    def set_fact(self, category: str, key: str, value: str, importance: int = 5) -> None:
        '''Upsert a user fact.'''
        self.db.execute_query(
            'INSERT OR REPLACE INTO user_facts (category, fact_key, fact_value, importance, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)',
            (category.lower(), key.lower(), str(value), int(importance)),
        )

    def get_all_facts(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        '''Query user_facts table from SQLite and return list of dicts with backend and C# DTO keys.'''
        if category:
            rows = self.db.fetch_all(
                'SELECT id, category, fact_key, fact_value, importance, updated_at FROM user_facts WHERE category = ? ORDER BY importance DESC, id DESC',
                (category.lower(),),
            )
        else:
            rows = self.db.fetch_all(
                'SELECT id, category, fact_key, fact_value, importance, updated_at FROM user_facts ORDER BY importance DESC, id DESC',
                (),
            )

        facts: List[Dict[str, Any]] = []
        for r in rows:
            row_dict = dict(r)
            f_id = int(row_dict.get('id', 0))
            f_cat = str(row_dict.get('category', 'user'))
            f_key = str(row_dict.get('fact_key', ''))
            f_val = str(row_dict.get('fact_value', ''))
            f_imp = int(row_dict.get('importance', 5)) if row_dict.get('importance') is not None else 5
            f_upd = str(row_dict.get('updated_at', ''))
            facts.append({
                'id': f_id,
                'category': f_cat,
                'key': f_key,
                'fact_key': f_key,
                'value': f_val,
                'fact_value': f_val,
                'importance': f_imp,
                'updated_at': f_upd,
            })
        return facts

    # Alias list_facts on MemoryManager
    def list_facts(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        '''Alias for get_all_facts.'''
        return self.get_all_facts(category=category)

    def get_facts_prompt_block(self) -> str:
        '''Return a formatted block of all facts for prompting.'''
        rows = self.db.fetch_all('SELECT * FROM user_facts ORDER BY importance DESC')
        if not rows:
            return ''
        lines = ['[FACTS]']
        for row in rows:
            cat = dict(row).get('category', '')
            key_ = dict(row).get('fact_key', '')
            val = dict(row).get('fact_value', '')
            imp = int(dict(row).get('importance', 0))
            lines.append(f'  {cat}:{key_}={val} (importance={imp})')
        return '\n'.join(lines)

    def delete_fact(
        self,
        identifier: Optional[Union[int, str]] = None,
        fact_id: Optional[int] = None,
        fact_key: Optional[str] = None,
    ) -> bool:
        '''Delete a fact by numeric id OR string fact_key from user_facts.'''
        # Check keyword argument fact_id
        if fact_id is not None:
            row = self.db.fetch_one('SELECT id FROM user_facts WHERE id = ?', (fact_id,))
            if row is not None:
                self.db.execute_query('DELETE FROM user_facts WHERE id = ?', (fact_id,))
                return True
            return False

        # Check keyword argument fact_key
        if fact_key is not None:
            row = self.db.fetch_one('SELECT id FROM user_facts WHERE fact_key = ?', (fact_key.lower(),))
            if row is not None:
                self.db.execute_query('DELETE FROM user_facts WHERE fact_key = ?', (fact_key.lower(),))
                return True
            return False

        if identifier is None:
            return False

        # Handle numeric identifier or integer string (e.g. 12 or "12")
        target_id: Optional[int] = None
        if isinstance(identifier, int):
            target_id = identifier
        elif isinstance(identifier, str) and identifier.isdigit():
            target_id = int(identifier)

        if target_id is not None:
            row = self.db.fetch_one('SELECT id FROM user_facts WHERE id = ?', (target_id,))
            if row is not None:
                self.db.execute_query('DELETE FROM user_facts WHERE id = ?', (target_id,))
                return True

        # Try deleting by fact_key string
        key_str = str(identifier).lower()
        row = self.db.fetch_one('SELECT id FROM user_facts WHERE fact_key = ?', (key_str,))
        if row is not None:
            self.db.execute_query('DELETE FROM user_facts WHERE fact_key = ?', (key_str,))
            return True

        return False

    def delete_fact_by_id(self, fact_id: int) -> bool:
        '''Delete a fact explicitly by numeric id.'''
        return self.delete_fact(identifier=fact_id)

    def clear_history(self, session_id: Optional[str] = None) -> int:
        '''Clear conversation history; optionally for a single session.'''
        if session_id is not None:
            self.db.execute_query('DELETE FROM conversations WHERE session_id = ?', (session_id,))
        else:
            self.db.execute_query('DELETE FROM conversations', ())
        return 0

    def clear_conversations(self, session_id: Optional[str] = None) -> int:
        '''Alias for clear_history.'''
        return self.clear_history(session_id=session_id)

    def cleanup_old_memories(self, days: int = 365) -> int:
        '''Remove messages older than days days.'''
        cutoff = datetime.now() - timedelta(days=days)
        ts = cutoff.strftime('%Y-%m-%d %H:%M:%S')
        self.db.execute_query('DELETE FROM conversations WHERE timestamp < ?', (ts,))
        return 0

    def set_preference(self, key: str, value: Any) -> None:
        '''Store a preference.'''
        self.db.execute_query(
            'INSERT OR REPLACE INTO preferences (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
            (key.lower(), str(value)),
        )

    def get_preference(self, key: str, default: Optional[str] = None) -> Any:
        '''Retrieve a preference by key.'''
        row = self.db.fetch_one('SELECT * FROM preferences WHERE key = ?', (key.lower(),))
        if row is None:
            return default
        return dict(row).get('value', default)

    def get_all_preferences(self) -> Dict[str, Any]:
        '''Retrieve all preferences as a key-value dictionary.'''
        rows = self.db.fetch_all('SELECT key, value FROM preferences ORDER BY key ASC')
        return {str(r['key']): str(r['value']) for r in rows}

    def list_preferences(self) -> List[Dict[str, Any]]:
        '''Retrieve all preferences as a list of dicts.'''
        rows = self.db.fetch_all('SELECT key, value, updated_at FROM preferences ORDER BY key ASC')
        return [{'key': str(r['key']), 'value': str(r['value']), 'updated_at': str(r['updated_at'])} for r in rows]

    def get_stats(self) -> Dict[str, int]:
        '''Return counts of conversations, facts, preferences, and audit logs.'''
        conv_row = self.db.fetch_one('SELECT COUNT(*) AS cnt FROM conversations')
        facts_row = self.db.fetch_one('SELECT COUNT(*) AS cnt FROM user_facts')
        prefs_row = self.db.fetch_one('SELECT COUNT(*) AS cnt FROM preferences')
        audit_row = self.db.fetch_one('SELECT COUNT(*) AS cnt FROM security_audit')
        return {
            'conversations': int(conv_row['cnt']) if conv_row else 0,
            'facts': int(facts_row['cnt']) if facts_row else 0,
            'preferences': int(prefs_row['cnt']) if prefs_row else 0,
            'audit_logs': int(audit_row['cnt']) if audit_row else 0,
        }


# Module-level convenience functions
def get_memory_manager() -> MemoryManager:
    '''Get the singleton MemoryManager instance.'''
    return MemoryManager.get_memory_manager()


def get_memory() -> MemoryManager:
    '''Alias for get_memory_manager.'''
    return MemoryManager.get_memory_manager()


if __name__ == '__main__':
    mm = MemoryManager()

    print('--- add_message ---')
    mm.add_message('s1', 'user', 'سلام جارویس')
    mm.add_message('s2', 'assistant', 'Hello JARVIS! How can I help?')
    rows = mm.get_recent_context(5)
    for r in rows:
        print(dict(r))

    print('\n--- search ---')
    hits = mm.search_conversations('سلام')
    print(f'Found {len(hits)} results')

    print('\n--- facts ---')
    mm.set_fact('person', 'name', 'Ali', 8)
    mm.set_fact('preference', 'color', 'red', 3)
    block = mm.get_facts_prompt_block()
    print(block if block else '(empty)')

    all_facts = mm.get_all_facts()
    print(f'All facts count: {len(all_facts)}')
    if all_facts:
        print('Sample fact:', all_facts[0])

    print('\n--- delete fact ---')
    deleted = mm.delete_fact(fact_key='name')
    print(f'Deleted name fact: {deleted}')

    print('\n--- preferences ---')
    mm.set_preference('theme', 'dark')
    val = mm.get_preference('theme')
    print(f'theme={val}')
    all_prefs = mm.get_all_preferences()
    print(f'All preferences: {all_prefs}')

    print('\n--- stats ---')
    print('Stats:', mm.get_stats())

    print('\n--- cleanup & clear ---')
    mm.cleanup_old_memories(30)
    print('cleanup done')
    mm.clear_history()
    print('history cleared')
