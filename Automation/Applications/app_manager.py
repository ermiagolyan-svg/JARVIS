# JARVIS Application Manager Module
import os, sys, subprocess, logging, time, json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from enum import IntEnum

_logger = logging.getLogger("jarvis.app_manager")
if not _logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[APP_MGR] %(message)s"))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)

# Known application paths for safe launching
KNOWN_APPS: Dict[str, str] = {
    "notepad": r"C:\Windows\System32\notepad.exe",
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "explorer": r"C:\Windows\explorer.exe",
    "cmd": r"C:\Windows\System32\cmd.exe",
    "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "taskmgr": r"C:\Windows\System32\taskmgr.exe",
    "calc": r"C:\Windows\System32\calc.exe",
    "ms-settings": r"C:\Windows\System32\ms-settings.exe",
}

class AppAction(IntEnum):
    LAUNCH = 1; CLOSE = 2; KILL = 3; STATUS = 4; LIST = 5

class AppManager:
    """Manage application launch, close, and status via subprocess + psutil."""
    def __init__(self) -> None:
        self._known_apps = KNOWN_APPS
        self._launch_log: List[Dict[str, Any]] = []

    def launch(self, app_name: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        path = self._known_apps.get(app_name.lower())
        if not path:
            return {"success": False, "error": f"Unknown app: {app_name}"}
        try:
            cmd = [path] + (args or [])
            _logger.info("Launching: %s", " ".join(cmd))
            proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            self._launch_log.append({"app": app_name, "pid": proc.pid, "time": time.time()})
            return {"success": True, "pid": proc.pid, "command": " ".join(cmd)}
        except Exception as exc:
            _logger.error("Launch failed (%s): %s", app_name, exc)
            return {"success": False, "error": str(exc)}

    def launch_app(self, name: str) -> Dict[str, Any]:
        """Router-compatible wrapper for launch()."""
        return self.launch(name)

    def close_app(self, name: str) -> Dict[str, Any]:
        """Router-compatible wrapper for close()."""
        return self.close(name)

    def get_status(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Return running apps status or specific app status."""
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"success": False, "error": "tasklist failed"}
            lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
            running = []
            for line in lines:
                parts = [p.strip('"') for p in line.split(",")]
                if len(parts) >= 2:
                    running.append({"name": parts[0], "pid": parts[1]})
            if name:
                found = [r for r in running if r["name"].lower() == name.lower()]
                return {"success": True, "running": bool(found), "apps": found}
            return {"success": True, "count": len(running), "apps": running[:20]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "tasklist timed out"}
        except Exception as exc:
            _logger.error("Get status failed: %s", exc)
            return {"success": False, "error": str(exc)}

    def get_known_apps(self) -> Dict[str, Any]:
        """Return the known applications dictionary."""
        return {"success": True, "apps": dict(self._known_apps)}

    def close(self, app_name: str) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", f"{app_name}.exe"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                _logger.info("Closed: %s", app_name)
                return {"success": True, "action": "close", "target": app_name}
            return {"success": False, "error": result.stderr.strip()[:200]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Close timed out"}
        except Exception as exc:
            _logger.error("Close failed (%s): %s", app_name, exc)
            return {"success": False, "error": str(exc)}

    def kill(self, app_name: str) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/IM", f"{app_name}.exe"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                _logger.info("Killed process tree: %s", app_name)
                return {"success": True, "action": "kill", "target": app_name}
            return {"success": False, "error": result.stderr.strip()[:200]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Kill timed out"}
        except Exception as exc:
            _logger.error("Kill failed (%s): %s", app_name, exc)
            return {"success": False, "error": str(exc)}

    def status(self, app_name: str) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {app_name}.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                return {"success": True, "running": True, "processes": len(lines)}
            return {"success": True, "running": False}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Status check timed out"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def list_apps(self) -> List[str]:
        return sorted(self._known_apps.keys())

    def get_launch_log(self) -> List[Dict[str, Any]]:
        return list(self._launch_log)


_instance: Optional[AppManager] = None

def get_app_manager() -> AppManager:
    global _instance
    if _instance is None:
        _instance = AppManager()
    return _instance


if __name__ == "__main__":
    print("=" * 70)
    print("JARVIS Application Manager - Self Test")
    print("=" * 70)
    am = get_app_manager()
    print(f"\n[KNOWN APPS] ({len(am.list_apps())} entries)")
    for app in am.list_apps():
        print(f"  - {app}")
    print("\n[LAUNCH TEST - notepad]")
    r = am.launch("notepad")
    print(f"  success={r['success']}, pid={r.get('pid')}")
    time.sleep(0.5)
    print("\n[STATUS CHECK - notepad]")
    r = am.status("notepad")
    print(f"  running={r.get('running')}, processes={r.get('processes')}")
    print("\n[LAUNCH LOG]")
    for entry in am.get_launch_log():
        print(f"  {entry['app']} -> pid={entry['pid']}")
    print("\n" + "=" * 70)
    print("Application Manager self-test complete.")
    print("=" * 70)
