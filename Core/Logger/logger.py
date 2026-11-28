"""
OMEGA Unified Central Logger & Observability Pipeline
==============================================================
Single Source of Truth for all Python modules, FastAPI/Uvicorn, background threads,
and managed subprocesses (GooyaWorker, llama-server).

Features:
- Single root logging.Handler interceptor (zero duplicate file handlers on Windows).
- Rotating unified text log (Logs/omega.log), error-only log (Logs/errors.log),
  and structured JSONL log (Logs/omega.jsonl).
- Thread-safe in-memory Ring Buffer (2000 structured entries with monotonic ID,
  real local/ISO timestamps, level, module, thread, turn_id, message, traceback).
- ContextVar-based end-to-end turn correlation (turn_id) and stage latency profiler.
- Global sys.excepthook and threading.excepthook installers.
- Non-blocking subprocess stream pumper supporting structured OMEGA_LOG JSON
  and intelligent plain-text level/traceback detection.
- Optional real-time listener hooks for WebSocket live log streaming.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "Logs"
_UNIFIED_LOG = _LOG_DIR / "omega.log"
_ERROR_LOG = _LOG_DIR / "errors.log"
_JSONL_LOG = _LOG_DIR / "omega.jsonl"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_RING_BUFFER_MAX = 2000
_ring_buffer: deque = deque(maxlen=_RING_BUFFER_MAX)
_buffer_lock = threading.RLock()
_seq_counter: int = 0

_setup_lock = threading.Lock()
_initialized: bool = False
_hooks_installed: bool = False
_unified_handler: Optional["_UnifiedLogHandler"] = None

_listeners_lock = threading.Lock()
_log_listeners: List[Callable[[Dict[str, Any]], None]] = []

_turn_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("omega_turn_id", default="-")
_tls = threading.local()

_NOISY_POLL_RE = re.compile(
    r'(?:"GET /api/v1/(?:health|status|telemetry|security/pending|logs(?:/modules)?)(?:\?[^"]*)? HTTP/[\d.]+"\s+200|GET http://testserver/api/v1/health "HTTP/[\d.]+\s+200)'
)

_LEVEL_NAME_TO_NO = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
}


# ---------------------------------------------------------------------------
# Turn Correlation & Latency Profiling
# ---------------------------------------------------------------------------
def new_turn_id(prefix: str = "T") -> str:
    """Create a short, readable correlation ID for a single user/system turn."""
    clean_prefix = (prefix or "T").upper()[:6]
    return f"{clean_prefix}-{uuid.uuid4().hex[:6]}"


def get_turn_id() -> str:
    """Get active correlation turn_id for the current execution context."""
    return _turn_id_var.get() or "-"


def set_turn_id(turn_id: Optional[str]) -> contextvars.Token:
    """Set correlation turn_id in current ContextVar and return reset token."""
    return _turn_id_var.set((turn_id or "-").strip() or "-")


def reset_turn_id(token: contextvars.Token) -> None:
    try:
        _turn_id_var.reset(token)
    except Exception:
        pass


@contextmanager
def turn_context(turn_id: Optional[str] = None, prefix: str = "T") -> Iterator[str]:
    """Context manager that binds a turn_id for all logs emitted inside the block."""
    tid = (turn_id or "").strip()
    if not tid or tid == "-":
        tid = new_turn_id(prefix)
    token = set_turn_id(tid)
    try:
        yield tid
    finally:
        reset_turn_id(token)


class TurnTimer:
    """Lightweight stage latency tracker for end-to-end turn profiling."""

    def __init__(self, turn_id: Optional[str] = None) -> None:
        self.turn_id = turn_id or get_turn_id()
        self._start = time.perf_counter()
        self._last = self._start
        self.stages_ms: Dict[str, float] = {}

    def mark(self, stage_name: str, elapsed_ms: Optional[float] = None) -> float:
        now = time.perf_counter()
        ms = round((now - self._last) * 1000.0, 1) if elapsed_ms is None else round(float(elapsed_ms), 1)
        self._last = now
        self.stages_ms[stage_name] = ms
        return ms

    def total_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000.0, 1)

    def format_summary(self) -> str:
        parts = [f"{k}={v:.1f}ms" for k, v in self.stages_ms.items()]
        parts.append(f"total={self.total_ms():.1f}ms")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Real-time Listeners (for WebSocket streaming)
# ---------------------------------------------------------------------------
def register_log_listener(callback: Callable[[Dict[str, Any]], None]) -> None:
    with _listeners_lock:
        if callback not in _log_listeners:
            _log_listeners.append(callback)


def unregister_log_listener(callback: Callable[[Dict[str, Any]], None]) -> None:
    with _listeners_lock:
        if callback in _log_listeners:
            _log_listeners.remove(callback)


# ---------------------------------------------------------------------------
# Unified Root Handler
# ---------------------------------------------------------------------------
class _SafeConsoleStreamHandler(logging.StreamHandler):
    """Console handler resilient to Windows legacy codepages when printing Persian UTF-8."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            stream = self.stream
            if stream is None:
                return
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                enc = getattr(stream, "encoding", None) or "utf-8"
                safe_bytes = (msg + self.terminator).encode(enc, errors="replace")
                if hasattr(stream, "buffer"):
                    stream.buffer.write(safe_bytes)
                else:
                    stream.write(safe_bytes.decode(enc, errors="replace"))
            self.flush()
        except Exception:
            pass


class _UnifiedLogHandler(logging.Handler):
    """Central thread-safe handler writing to unified rotating files, JSONL, console, and ring buffer."""

    def __init__(self, console_level: int = logging.INFO, file_level: int = logging.DEBUG) -> None:
        super().__init__(level=logging.DEBUG)
        self._io_lock = threading.RLock()
        self._console_level = console_level
        self._file_level = file_level

        _LOG_DIR.mkdir(parents=True, exist_ok=True)

        self._file_handler = RotatingFileHandler(
            str(_UNIFIED_LOG), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
        )
        self._file_handler.setLevel(file_level)

        self._error_handler = RotatingFileHandler(
            str(_ERROR_LOG), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
        )
        self._error_handler.setLevel(logging.ERROR)

        self._jsonl_handler = RotatingFileHandler(
            str(_JSONL_LOG), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
        )
        self._jsonl_handler.setLevel(file_level)

        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        self._console_handler = _SafeConsoleStreamHandler(sys.stdout)
        self._console_handler.setLevel(console_level)

        plain_fmt = logging.Formatter("%(message)s")
        self._file_handler.setFormatter(plain_fmt)
        self._error_handler.setFormatter(plain_fmt)
        self._jsonl_handler.setFormatter(plain_fmt)
        self._console_handler.setFormatter(plain_fmt)

    @staticmethod
    def _clean_module_name(record: logging.LogRecord) -> str:
        override = getattr(record, "omega_module", None)
        if override:
            return str(override)
        name = record.name or "root"
        if name == "root":
            return record.module or "root"
        return name

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_tls, "in_emit", False):
            return
        _tls.in_emit = True
        try:
            global _seq_counter

            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)

            level_no = record.levelno
            level_name = record.levelname
            module_name = self._clean_module_name(record)
            thread_name = record.threadName or threading.current_thread().name
            turn_id = getattr(record, "turn_id", None) or get_turn_id() or "-"

            # Suppress repetitive 200 OK UI polling noise from INFO/console/ring-buffer
            is_poll_noise = (module_name in ("uvicorn.access", "httpx", "httpx2") and bool(_NOISY_POLL_RE.search(message)))
            if is_poll_noise:
                level_no = logging.DEBUG
                level_name = "DEBUG"

            tb_text = getattr(record, "omega_tb", None) or ""
            if not tb_text and record.exc_info:
                try:
                    tb_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
                except Exception:
                    tb_text = ""
            elif not tb_text and record.exc_text:
                tb_text = str(record.exc_text).rstrip()

            dt = datetime.fromtimestamp(record.created).astimezone()
            ts_display = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}"
            ts_iso = dt.isoformat(timespec="milliseconds")

            formatted_line = (
                f"{ts_display} | {level_name:<8} | {module_name:<18} | {turn_id:<9} | [{thread_name}] {message}"
            )
            if tb_text:
                formatted_line = f"{formatted_line}\n{tb_text}"

            entry: Optional[Dict[str, Any]] = None

            with self._io_lock:
                # 1. File outputs (always write unified + jsonl; errors.log for ERROR+)
                if level_no >= self._file_level:
                    raw_rec = logging.LogRecord(
                        name=module_name,
                        level=level_no,
                        pathname=record.pathname,
                        lineno=record.lineno,
                        msg=formatted_line,
                        args=(),
                        exc_info=None,
                    )
                    self._file_handler.emit(raw_rec)

                    if level_no >= logging.ERROR:
                        self._error_handler.emit(raw_rec)

                # 2. Console output
                if level_no >= self._console_level and not is_poll_noise:
                    console_rec = logging.LogRecord(
                        name=module_name,
                        level=level_no,
                        pathname=record.pathname,
                        lineno=record.lineno,
                        msg=formatted_line,
                        args=(),
                        exc_info=None,
                    )
                    self._console_handler.emit(console_rec)

                # 3. Ring Buffer & JSONL (skip repetitive polling 200 OK noise)
                if not is_poll_noise:
                    with _buffer_lock:
                        _seq_counter += 1
                        entry = {
                            "id": _seq_counter,
                            "timestamp": ts_display,
                            "iso_timestamp": ts_iso,
                            "level": level_name,
                            "level_no": level_no,
                            "module": module_name,
                            "thread": thread_name,
                            "turn_id": turn_id,
                            "message": message,
                            "traceback": tb_text,
                        }
                        _ring_buffer.append(entry)

                    if level_no >= self._file_level:
                        jsonl_line = json.dumps(entry, ensure_ascii=False)
                        jsonl_rec = logging.LogRecord(
                            name=module_name,
                            level=level_no,
                            pathname=record.pathname,
                            lineno=record.lineno,
                            msg=jsonl_line,
                            args=(),
                            exc_info=None,
                        )
                        self._jsonl_handler.emit(jsonl_rec)

            # 4. Notify live listeners outside locks
            if entry is not None:
                with _listeners_lock:
                    listeners = list(_log_listeners)
                for cb in listeners:
                    try:
                        cb(entry)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            _tls.in_emit = False


def setup_unified_logging(
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> _UnifiedLogHandler:
    """Attach the single unified handler to Python's root logger (idempotent)."""
    global _initialized, _unified_handler
    with _setup_lock:
        root = logging.getLogger()
        if not _initialized or _unified_handler is None:
            _unified_handler = _UnifiedLogHandler(console_level=console_level, file_level=file_level)
            # Remove pre-existing StreamHandlers/FileHandlers on root to avoid duplicate lines
            for h in list(root.handlers):
                if h is not _unified_handler:
                    root.removeHandler(h)
            root.addHandler(_unified_handler)
            root.setLevel(logging.DEBUG)
            _initialized = True
        elif _unified_handler not in root.handlers:
            root.addHandler(_unified_handler)
            root.setLevel(logging.DEBUG)

        # Ensure FastAPI / Uvicorn propagate cleanly into our single root handler
        for web_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "starlette"):
            wl = logging.getLogger(web_logger_name)
            wl.handlers.clear()
            wl.propagate = True
            wl.setLevel(logging.INFO)

        # Quiet noisy third-party transport libraries
        for noisy in ("urllib3", "httpcore", "httpx", "httpx2", "huggingface_hub", "watchfiles", "multipart", "asyncio", "PIL"):
            nl = logging.getLogger(noisy)
            if nl.level == logging.NOTSET or nl.level < logging.WARNING:
                nl.setLevel(logging.WARNING)

        return _unified_handler


def install_exception_hooks() -> None:
    """Install sys.excepthook and threading.excepthook to capture uncaught crashes."""
    global _hooks_installed
    setup_unified_logging()
    with _setup_lock:
        if _hooks_installed:
            return

        _orig_sys_hook = sys.excepthook

        def _omega_sys_excepthook(exc_type, exc_value, exc_tb) -> None:
            if issubclass(exc_type, KeyboardInterrupt):
                _orig_sys_hook(exc_type, exc_value, exc_tb)
                return
            logging.getLogger("uncaught.main").critical(
                "Uncaught exception in main thread: %s",
                exc_value,
                exc_info=(exc_type, exc_value, exc_tb),
            )

        sys.excepthook = _omega_sys_excepthook

        if hasattr(threading, "excepthook"):
            def _omega_thread_excepthook(args: threading.ExceptHookArgs) -> None:
                if args.exc_type is not None and issubclass(args.exc_type, SystemExit):
                    return
                t_name = args.thread.name if args.thread else "unknown-thread"
                logging.getLogger("uncaught.thread").critical(
                    "Uncaught exception in background thread '%s': %s",
                    t_name,
                    args.exc_value,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )

            threading.excepthook = _omega_thread_excepthook

        _hooks_installed = True


# ---------------------------------------------------------------------------
# Subprocess Non-Blocking Stream Log Bridge
# ---------------------------------------------------------------------------
def _detect_plain_level(line: str, default_level: int = logging.INFO) -> Tuple[int, str]:
    stripped = line.strip()
    upper = stripped.upper()

    for prefix, lvl in (
        ("CRITICAL:", logging.CRITICAL),
        ("FATAL:", logging.CRITICAL),
        ("ERROR:", logging.ERROR),
        ("WARNING:", logging.WARNING),
        ("WARN:", logging.WARNING),
        ("INFO:", logging.INFO),
        ("DEBUG:", logging.DEBUG),
        ("[ERROR]", logging.ERROR),
        ("[WARN]", logging.WARNING),
        ("[WARNING]", logging.WARNING),
        ("[INFO]", logging.INFO),
        ("[DEBUG]", logging.DEBUG),
    ):
        if upper.startswith(prefix):
            return lvl, stripped[len(prefix):].strip()

    if any(tok in upper for tok in ("TRACEBACK (MOST RECENT CALL LAST)", "OUTOFMEMORY", "CUDA ERROR", "RUNTIMEERROR:", "SEGFAULT", "ASSERTIONERROR:")):
        return logging.ERROR, stripped
    if "WARNING" in upper and not upper.startswith("INFO"):
        return logging.WARNING, stripped
    return default_level, stripped


def pump_subprocess_stream(
    stream: Any,
    module_name: str,
    default_level: int = logging.INFO,
    json_prefix: str = "OMEGA_LOG:",
    thread_name: Optional[str] = None,
) -> threading.Thread:
    """
    Continuously drain a subprocess text stream (stderr/stdout) in a daemon thread
    without blocking the parent process or risking Windows pipe deadlocks.
    Supports structured JSON lines (OMEGA_LOG:{...}) and plain-text fallback with
    multi-line Python traceback aggregation.
    """
    setup_unified_logging()
    target_logger = logging.getLogger(module_name)

    def _emit_line(level_no: int, msg: str, mod: str, turn_id: str, tb: str = "") -> None:
        if not msg and not tb:
            return
        extra = {
            "omega_module": mod or module_name,
            "turn_id": turn_id or "-",
        }
        if tb:
            extra["omega_tb"] = tb
        target_logger.log(level_no, msg or "(subprocess traceback)", extra=extra)

    def _reader() -> None:
        tb_buffer: List[str] = []
        in_tb = False

        def _flush_tb() -> None:
            nonlocal tb_buffer, in_tb
            if tb_buffer:
                last_line = tb_buffer[-1].strip()
                tb_full = "\n".join(tb_buffer)
                _emit_line(logging.ERROR, f"Subprocess exception: {last_line}", module_name, "-", tb=tb_full)
                tb_buffer = []
            in_tb = False

        try:
            if stream is None:
                return
            for raw_line in iter(stream.readline, ""):
                line = raw_line.rstrip("\r\n")
                if not line.strip():
                    if in_tb:
                        _flush_tb()
                    continue

                # Structured JSON log protocol from worker subprocesses
                if json_prefix and line.startswith(json_prefix):
                    if in_tb:
                        _flush_tb()
                    payload_str = line[len(json_prefix):].strip()
                    try:
                        data = json.loads(payload_str)
                        lvl_str = str(data.get("level", "INFO")).upper()
                        lvl_no = _LEVEL_NAME_TO_NO.get(lvl_str, default_level)
                        msg = str(data.get("msg", data.get("message", "")))
                        mod = str(data.get("module") or module_name)
                        tid = str(data.get("turn_id") or "-")
                        tb = str(data.get("tb") or data.get("traceback") or "")
                        _emit_line(lvl_no, msg, mod, tid, tb=tb)
                        continue
                    except Exception:
                        pass

                # Aggregate multi-line Python tracebacks on plain-text streams
                if line.startswith("Traceback (most recent call last):"):
                    if in_tb:
                        _flush_tb()
                    in_tb = True
                    tb_buffer = [line]
                    continue

                if in_tb:
                    if line.startswith(" ") or line.startswith("\t"):
                        tb_buffer.append(line)
                        continue
                    else:
                        tb_buffer.append(line)
                        _flush_tb()
                        continue

                lvl_no, cleaned = _detect_plain_level(line, default_level=default_level)
                _emit_line(lvl_no, cleaned, module_name, "-")

            if in_tb:
                _flush_tb()
        except Exception as exc:
            target_logger.debug("Subprocess stream pumper exited (%s): %s", module_name, exc)

    t = threading.Thread(
        target=_reader,
        daemon=True,
        name=thread_name or f"LogPump-{module_name}",
    )
    t.start()
    return t


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# OmegaLogger Facade & Public API
# ---------------------------------------------------------------------------
class OmegaLogger:
    """Thin facade over stdlib logging routed to the unified root pipeline."""

    def __init__(self, name: str = "omega") -> None:
        setup_unified_logging()
        self._name = name or "omega"
        self._logger = logging.getLogger(self._name)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = True

    def _emit(
        self,
        level: int,
        msg: Any,
        *args: Any,
        module: Optional[str] = None,
        turn_id: Optional[str] = None,
        exc_info: Any = None,
        stack_info: bool = False,
        extra: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        merged_extra: Dict[str, Any] = dict(extra) if extra else {}
        if module:
            merged_extra["omega_module"] = module
        if turn_id:
            merged_extra["turn_id"] = turn_id
        self._logger.log(
            level,
            msg,
            *args,
            exc_info=exc_info,
            stack_info=stack_info,
            extra=merged_extra or None,
        )

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.WARNING, msg, *args, **kwargs)

    def warn(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._emit(logging.CRITICAL, msg, *args, **kwargs)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        self._emit(logging.ERROR, msg, *args, **kwargs)

    def get_logger(self) -> logging.Logger:
        return self._logger

    def get_recent_logs(
        self,
        limit: int = 200,
        level: Optional[str] = None,
        module: Optional[str] = None,
        search: Optional[str] = None,
        since_id: int = 0,
    ) -> List[Dict[str, Any]]:
        return get_recent_logs(limit=limit, level=level, module=module, search=search, since_id=since_id)


_logger_cache: Dict[str, OmegaLogger] = {}
_cache_lock = threading.Lock()


def get_logger(module_name: str = "omega") -> OmegaLogger:
    """Get or create a thread-safe OmegaLogger bound to the unified pipeline."""
    setup_unified_logging()
    key = (module_name or "omega").strip() or "omega"
    with _cache_lock:
        if key not in _logger_cache:
            _logger_cache[key] = OmegaLogger(key)
        return _logger_cache[key]


def get_recent_logs(
    limit: int = 200,
    level: Optional[str] = None,
    module: Optional[str] = None,
    search: Optional[str] = None,
    since_id: int = 0,
) -> List[Dict[str, Any]]:
    """Return structured, chronological log entries from the unified in-memory ring buffer."""
    with _buffer_lock:
        items = list(_ring_buffer)

    if since_id and since_id > 0:
        items = [x for x in items if x["id"] > since_id]

    if level:
        lvl_upper = level.strip().upper()
        if lvl_upper and lvl_upper != "ALL":
            if lvl_upper.endswith("+"):
                min_no = _LEVEL_NAME_TO_NO.get(lvl_upper[:-1], logging.DEBUG)
                items = [x for x in items if x["level_no"] >= min_no]
            else:
                items = [x for x in items if x["level"].upper() == lvl_upper]

    if module:
        mod_q = module.strip().lower()
        if mod_q and mod_q != "all":
            items = [x for x in items if mod_q in x["module"].lower()]

    if search:
        q = search.strip().lower()
        if q:
            items = [
                x for x in items
                if q in x["message"].lower()
                or q in x["module"].lower()
                or q in x["turn_id"].lower()
                or q in x["traceback"].lower()
            ]

    if limit and limit > 0:
        items = items[-int(limit):]
    return items


def get_buffered_modules() -> List[str]:
    """Return sorted unique module names currently present in the ring buffer."""
    with _buffer_lock:
        return sorted({x["module"] for x in _ring_buffer})


def get_last_log_id() -> int:
    with _buffer_lock:
        return _seq_counter


def get_recent_logs_count() -> int:
    with _buffer_lock:
        return len(_ring_buffer)


def clear_recent_logs() -> None:
    with _buffer_lock:
        _ring_buffer.clear()


# Automatically attach unified logging on module import
setup_unified_logging()