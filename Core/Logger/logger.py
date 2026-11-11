"""
JARVIS Core Logger
==================
Rotating file + console logger with in-memory ring buffer for live log retrieval.
"""

import logging
import os
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import List, Optional


# Absolute paths
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "Logs")
_CORE_LOG = os.path.join(_LOG_DIR, "jarvis_core.log")
_ERROR_LOG = os.path.join(_LOG_DIR, "errors.log")

# Ensure log directory exists
os.makedirs(_LOG_DIR, exist_ok=True)

# In-memory ring buffer (max 500 entries)
_recent_logs: deque = deque(maxlen=500)
_logs_lock = threading.Lock()


class JarvisLogger:
    """Rotating file and console logger for JARVIS core."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)

        # Avoid duplicate handlers on repeated calls
        if self._logger.handlers:
            return

        # Formatter
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Console handler
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        self._logger.addHandler(console)

        # Core log handler (DEBUG+)
        core_handler = RotatingFileHandler(
            _CORE_LOG, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        core_handler.setLevel(logging.DEBUG)
        core_handler.setFormatter(fmt)
        self._logger.addHandler(core_handler)

        # Error log handler (ERROR+)
        error_handler = RotatingFileHandler(
            _ERROR_LOG, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(fmt)
        self._logger.addHandler(error_handler)

    def _log(self, level: int, msg: str) -> None:
        self._logger.log(level, msg)
        with _logs_lock:
            _recent_logs.append(f"{level} | {msg}")

    def debug(self, msg: str) -> None:
        self._log(logging.DEBUG, msg)

    def info(self, msg: str) -> None:
        self._log(logging.INFO, msg)

    def warning(self, msg: str) -> None:
        self._log(logging.WARNING, msg)

    def error(self, msg: str) -> None:
        self._log(logging.ERROR, msg)

    def critical(self, msg: str) -> None:
        self._log(logging.CRITICAL, msg)

    def get_logger(self) -> logging.Logger:
        """Return the underlying stdlib Logger for advanced use cases."""
        return self._logger


# Module-level singleton cache
_logger_cache: dict = {}
_cache_lock = threading.Lock()


def get_logger(module_name: str) -> JarvisLogger:
    """Get or create a JarvisLogger instance for the given module name (thread-safe)."""
    with _cache_lock:
        if module_name not in _logger_cache:
            _logger_cache[module_name] = JarvisLogger(module_name)
        return _logger_cache[module_name]


def get_recent_logs() -> List[str]:
    """Return a list of recent log entries from the in-memory ring buffer."""
    with _logs_lock:
        return list(_recent_logs)


def get_recent_logs_count() -> int:
    """Return the number of entries currently in the ring buffer."""
    with _logs_lock:
        return len(_recent_logs)


def clear_recent_logs() -> None:
    """Clear the in-memory ring buffer (useful for log rotation)."""
    with _logs_lock:
        _recent_logs.clear()
