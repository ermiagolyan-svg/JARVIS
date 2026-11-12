# JARVIS Watchdog - Supervisor for Core/jarvis_main.py
import os
import sys
import time
import signal
import subprocess
import logging
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent

# --- Logging setup ---
_logger = logging.getLogger("jarvis_watchdog")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("[WATCHDOG] %(asctime)s %(message)s", datefmt="%H:%M:%S"))
_logger.addHandler(_handler)
_logger.setLevel(logging.INFO)


def _build_watchdog_path() -> Path:
    return _project_root / "Core" / "jarvis_main.py"


def _check_api_health(timeout: float = 3.0) -> bool:
    """Check if /api/v1/status responds within timeout."""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8765/api/v1/status")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _run_jarvis_main(test_once: bool = False) -> int:
    """Execute Core/jarvis_main.py. Returns exit code."""
    watchdog_path = _build_watchdog_path()
    cmd = [str(_project_root / ".venv" / "Scripts" / "python.exe"), str(watchdog_path)]
    if test_once:
        cmd.append("--test-once")

    _logger.info("Starting jarvis_main.py (pid will be tracked)")
    proc = subprocess.Popen(
        cmd,
        cwd=str(_project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def run_watchdog(test_once: bool = False) -> None:
    """Main watchdog loop. Monitors jarvis_main.py and restarts on failure."""
    _logger.info("JARVIS Watchdog started")
    _logger.info("Watchdog path: %s", str(_build_watchdog_path()))
    _logger.info("Mode: %s", "test-once" if test_once else "continuous")

    backoff = 5.0
    max_backoff = 60.0
    restart_count = 0

    while True:
        proc = _run_jarvis_main(test_once)

        if test_once:
            # In test mode, wait for the process to finish naturally
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                code = -1
            _logger.info("Test run finished with exit code %d", code)
            break

        # Continuous monitoring: poll status endpoint every 5 seconds
        while True:
            if proc.poll() is not None:
                _logger.warning("jarvis_main.py exited with code %d", proc.returncode)
                break
            try:
                healthy = _check_api_health(timeout=2.0)
                if not healthy:
                    _logger.info("API /api/v1/status unhealthy (process still running)")
            except Exception as exc:
                _logger.debug("Health check error: %s", exc)
            time.sleep(5)

        restart_count += 1
        _logger.warning("Restart #%d | backoff %.0fs", restart_count, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    test_once = "--test-once" in sys.argv
    run_watchdog(test_once=test_once)