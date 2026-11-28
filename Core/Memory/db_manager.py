"""Thread-safe SQLite database manager for OMEGA memory persistence."""

import sqlite3
import threading
from pathlib import Path
from typing import Any, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = _PROJECT_ROOT / "Data" / "Memory" / "omega_memory.db"


class DatabaseManager:
    """Thread-safe SQLite database manager with automatic schema initialization and connection recovery."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._db_path = DB_PATH
        self._ensure_db_dir()
        self._init_schema()

    def _ensure_db_dir(self) -> None:
        """Ensure the database directory exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create a database connection with recovery logic."""
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.OperationalError:
                self._conn = None
                raise
        return self._conn

    def _recover_connection(self) -> sqlite3.Connection:
        """Attempt to recover a broken connection."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        return self._get_connection()

    def _init_schema(self) -> None:
        """Initialize the database schema on startup."""
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                language TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                fact_key TEXT UNIQUE,
                fact_value TEXT,
                importance INTEGER,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS security_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_type TEXT,
                risk_level TEXT,
                payload TEXT,
                status TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

    def execute_query(self, query: str, params: Tuple = ()) -> None:
        """Execute a write query (INSERT, UPDATE, DELETE)."""
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(query, params)
                conn.commit()
            except sqlite3.OperationalError:
                conn = self._recover_connection()
                conn.execute(query, params)
                conn.commit()

    def fetch_one(self, query: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        """Fetch a single row from the database."""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(query, params)
                return cursor.fetchone()
            except sqlite3.OperationalError:
                conn = self._recover_connection()
                cursor = conn.execute(query, params)
                return cursor.fetchone()

    def fetch_all(self, query: str, params: Tuple = ()) -> List[sqlite3.Row]:
        """Fetch all rows from the database."""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(query, params)
                return cursor.fetchall()
            except sqlite3.OperationalError:
                conn = self._recover_connection()
                cursor = conn.execute(query, params)
                return cursor.fetchall()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __del__(self) -> None:
        self.close()


# Module-level singleton
_db_instance: Optional[DatabaseManager] = None
_db_lock = threading.Lock()


def get_db() -> DatabaseManager:
    """Get or create the singleton DatabaseManager instance (thread-safe)."""
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            _db_instance = DatabaseManager()
        return _db_instance


def init_db() -> DatabaseManager:
    """Alias for get_db() - initializes the database on first call."""
    return get_db()


def get_db_path() -> Path:
    """Return the absolute path to the database file."""
    return DB_PATH