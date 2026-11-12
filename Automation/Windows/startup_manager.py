# JARVIS Windows Startup Manager
import os
import sys
import winreg
import subprocess
from pathlib import Path
from typing import Optional

_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REGISTRY_VALUE_NAME = "JARVIS_AI_System"
_TASK_NAME = "JARVIS_AI_System"
_JARVIS_WATCHDOG = "Core\\jarvis_watchdog.py"
_VENV_PYTHONW = ".venv\\Scripts\\pythonw.exe"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _build_entry_path() -> str:
    return str(_project_root() / _JARVIS_WATCHDOG)


def _build_vbs_entry_path() -> str:
    return str(_project_root() / "Installer" / "start_jarvis_hidden.vbs")


def register_startup() -> bool:
    project_root = _project_root()
    entry_path = str(project_root / _JARVIS_WATCHDOG)

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _REGISTRY_KEY,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        )
        winreg.SetValueEx(key, _REGISTRY_VALUE_NAME, 0, winreg.REG_SZ, entry_path)
        winreg.CloseKey(key)
    except OSError as exc:
        print(f"[startup_manager] Registry write failed: {exc}", file=sys.stderr)
        return False

    try:
        result = subprocess.run(
            [
                "schtasks",
                "/create",
                "/tn", _TASK_NAME,
                "/tr", f'"{entry_path}"',
                "/sc", "ONLOGON",
                "/ru", "CURRENT_USER",
                "/rl", "HIGHEST",
                "/f",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            print(f"[startup_manager] schtasks failed: {result.stderr.strip()}", file=sys.stderr)
            return False
    except subprocess.TimeoutExpired:
        print("[startup_manager] schtasks timed out", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"[startup_manager] schtasks error: {exc}", file=sys.stderr)
        return False

    return True


def is_registered() -> bool:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _REGISTRY_KEY,
            0,
            winreg.KEY_QUERY_VALUE,
        )
        value, _ = winreg.QueryValueEx(key, _REGISTRY_VALUE_NAME)
        winreg.CloseKey(key)
        registry_ok = bool(value) and "jarvis_watchdog" in str(value).lower()
    except (OSError, FileNotFoundError):
        registry_ok = False

    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", _TASK_NAME, "/fo", "CSV", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        task_ok = result.returncode == 0 and _TASK_NAME in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        task_ok = False

    return registry_ok and task_ok


def unregister_startup() -> bool:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _REGISTRY_KEY,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        )
        winreg.DeleteValue(key, _REGISTRY_VALUE_NAME)
        winreg.CloseKey(key)
    except (OSError, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", _TASK_NAME, "/f"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def get_startup_info() -> dict:
    return {
        "registry_key": f"HKCU\\{_REGISTRY_KEY}",
        "registry_value": _REGISTRY_VALUE_NAME,
        "task_name": _TASK_NAME,
        "watchdog_path": _build_entry_path(),
        "vbs_wrapper_path": _build_vbs_entry_path(),
        "is_registered": is_registered(),
    }


if __name__ == "__main__":
    print("=" * 70)
    print("JARVIS Startup Manager - Self Test (non-blocking)")
    print("=" * 70)

    info = get_startup_info()
    print(f"\n[PATH GENERATION]")
    print(f"  Registry key:     {info['registry_key']}")
    print(f"  Registry value:   {info['registry_value']}")
    print(f"  Task name:        {info['task_name']}")
    print(f"  Watchdog path:    {info['watchdog_path']}")
    print(f"  VBS wrapper:      {info['vbs_wrapper_path']}")

    print(f"\n[REGISTRY QUERY]")
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REGISTRY_KEY, 0, winreg.KEY_QUERY_VALUE
        )
        count = winreg.QueryInfoKey(key)[0]
        winreg.CloseKey(key)
        print(f"  Registry subkeys: {count} (read-only query OK)")
    except OSError as exc:
        print(f"  Registry query error: {exc}")

    print(f"\n[SCHTASKS QUERY]")
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", _TASK_NAME, "/fo", "CSV", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        exists = result.returncode == 0 and _TASK_NAME in result.stdout
        print(f"  Task exists: {exists}")
    except (subprocess.TimeoutExpired, OSError):
        print("  schtasks query failed (may need admin)")

    print(f"\n[STATUS]")
    print(f"  is_registered(): {info['is_registered']}")
    print("\n" + "=" * 70)
    print("Startup Manager self-test complete. No test entries left behind.")
    print("=" * 70)