# OMEGA Automation Router
# Wires system_control, app_manager, file_manager through SecurityPermissionManager
# with role-based routing and command execution.

import os
import sys
import logging
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Automation.Security.permission_manager import (
    SecurityPermissionManager,
    PermissionLevel,
    PermissionResult,
    get_permission_manager,
)
from Automation.Windows.system_control import SystemControl, get_system_control
from Automation.Applications.app_manager import AppManager, get_app_manager
from Automation.FileSystem.file_manager import FileManager, get_file_manager
from Automation.Web.web_search import WebSearchEngine, get_web_search_engine

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role definitions (IntEnum for ordering)
# ---------------------------------------------------------------------------
class Role(IntEnum):
    GUEST = 1
    USER = 2
    ADMIN = 3


ROLE_CAPABILITIES: Dict[Role, List[str]] = {
    Role.GUEST: ["volume get", "media play", "media pause", "status"],
    Role.USER: [
        "volume set", "volume mute", "volume up", "volume down",
        "media next", "media prev", "media toggle",
        "launch app", "close app", "app status", "list apps",
        "read file", "write file", "delete file", "scan sensitive",
        "list dir", "file search", "get log",
    ],
    Role.ADMIN: [
        "volume set", "volume mute", "volume up", "volume down",
        "media next", "media prev", "media toggle", "media play", "media pause",
        "launch app", "close app", "app status", "list apps",
        "read file", "write file", "delete file", "scan sensitive",
        "list dir", "file search", "get log",
        "system shutdown", "system reboot", "system sleep", "system lock",
        "brightness up", "brightness down", "brightness set",
    ],
}

# ---------------------------------------------------------------------------
# Route mapping: command prefix -> executor + method
# ---------------------------------------------------------------------------
ROUTE_TABLE = {
    # System control routes
    "volume": ("system_control", "set_volume"),
    "volume get": ("system_control", "get_volume"),
    "media": ("system_control", "toggle_media"),
    "brightness": ("system_control", "set_brightness"),
    # App routes
    "launch": ("app_manager", "launch_app"),
    "close": ("app_manager", "close_app"),
    "status": ("app_manager", "get_status"),
    "list apps": ("app_manager", "get_known_apps"),
    # File routes
    "read file": ("file_manager", "read_file"),
    "write file": ("file_manager", "write_file"),
    "delete file": ("file_manager", "delete_file"),
    "scan sensitive": ("file_manager", "scan_sensitive"),
    "list dir": ("file_manager", "list_dir"),
    "file search": ("file_manager", "search"),
    "get log": ("file_manager", "get_operation_log"),
    "web search": ("web_search", "search"),
    "google search": ("web_search", "search"),
}

SYSTEM_ROUTES = {
    "shutdown": ("system_control", "system_shutdown"),
    "reboot": ("system_control", "system_reboot"),
    "sleep": ("system_control", "system_sleep"),
    "lock": ("system_control", "system_lock"),
}


# ---------------------------------------------------------------------------
# Router class
# ---------------------------------------------------------------------------
class AutomationRouter:
    def __init__(self, role: Role = Role.USER):
        self.role = role
        self._perm_mgr = get_permission_manager()
        self._system = get_system_control()
        self._apps = get_app_manager()
        self._files = get_file_manager()
        self._web = get_web_search_engine()

    @property
    def permission_manager(self) -> SecurityPermissionManager:
        return self._perm_mgr

    def check_permission(self, command: str) -> PermissionResult:
        """Check if the current role can execute this command."""
        if not command.strip():
            return PermissionResult(False, Role.GUEST, "Empty command")
        cmd_lower = command.lower().strip()
        if any(cmd_lower.startswith(k) for k in SYSTEM_ROUTES):
            required_level = Role.ADMIN
        elif any(cmd_lower.startswith(k) for k in ["system ", "launch", "close"]):
            required_level = Role.USER
        elif any(cmd_lower.startswith(k) for k in ["read file", "write file", "delete file", "scan sensitive"]):
            required_level = Role.USER
        else:
            required_level = Role.GUEST
        if self.role >= required_level:
            return PermissionResult(True, self.role, f"Granted (role={self.role.name})")
        return PermissionResult(False, required_level, f"Denied: requires {required_level.name}, have {self.role.name}")

    def execute(self, command: str) -> Dict[str, Any]:
        """Execute a routed automation command."""
        if not command.strip():
            return {"success": False, "error": "Empty command"}
        result = self.check_permission(command)
        if not result.granted:
            _logger.warning("Permission denied: %s (%s)", command, result.reason)
            return {"success": False, "error": result.reason}
        cmd_lower = command.lower().strip()
        parts = cmd_lower.split(None, 1)
        args_str = parts[1] if len(parts) > 1 else ""

        # System routes
        for key, (module, method) in SYSTEM_ROUTES.items():
            if cmd_lower.startswith(key):
                return getattr(self._system, method)()

        # Route table lookup — find longest matching prefix
        matched = None
        for key in sorted(ROUTE_TABLE.keys(), key=len, reverse=True):
            if cmd_lower.startswith(key):
                matched = (key, ROUTE_TABLE[key])
                break
        if not matched:
            return {"success": False, "error": f"No route found for: {command}"}

        route_key, (module_name, method_name) = matched
        # Recompute args_str based on the actual matched route key
        args_str = cmd_lower[len(route_key):].strip() if len(cmd_lower) > len(route_key) else ""
        executor_map = {"system_control": self._system, "app_manager": self._apps, "file_manager": self._files, "web_search": self._web}
        executor = executor_map.get(module_name)
        if not executor:
            return {"success": False, "error": f"Unknown module: {module_name}"}
        method = getattr(executor, method_name, None)
        if not method:
            return {"success": False, "error": f"No method '{method_name}' on {module_name}"}

        try:
            if route_key == "volume":
                if args_str.strip().lower() == "get":
                    return method()
                # Handle "set <number>" format
                val = args_str.strip().lower()
                if "set" in val:
                    val = val.split()[-1]
                return method(int(val))
            elif route_key == "media":
                return method()
            elif route_key == "brightness":
                return method(int(args_str.strip()) if args_str.strip() else None)
            elif route_key in ("launch", "close"):
                return method(args_str.strip().lower())
            elif route_key == "status":
                return method(args_str.strip().lower() if args_str.strip() else None)
            elif route_key == "read file":
                return method(args_str.strip())
            elif route_key == "write file":
                path, content = args_str.split(None, 1) if " " in args_str else (args_str.strip(), "")
                return method(path.strip(), content)
            elif route_key == "delete file":
                return method(args_str.strip())
            elif route_key == "scan sensitive":
                return method(args_str.strip())
            elif route_key == "list dir":
                return method(args_str.strip() or str(Path.home()))
            elif route_key == "file search":
                return method(args_str.strip() if args_str.strip() else str(Path.home()))
            elif route_key in ("web search", "google search"):
                return method(args_str.strip())
            elif route_key == "get log":
                return method()
        except Exception as exc:
            _logger.error("Execution error (%s): %s", command, exc, exc_info=True)
            return {"success": False, "error": str(exc)}
        return {"success": True, "result": method()}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_instance: Optional[AutomationRouter] = None

def get_automation_router(role: Role = Role.USER) -> AutomationRouter:
    global _instance
    if _instance is None:
        _instance = AutomationRouter(role=role)
    return _instance


if __name__ == "__main__":
    print("=" * 70)
    print("OMEGA Automation Router - Self Test")
    print("=" * 70)
    router = get_automation_router(role=Role.USER)
    pm = router.permission_manager

    print("\n[PERMISSION CHECKS]")
    tests = [("volume set 50", True), ("launch notepad", True), ("read file C:\\test.txt", True), ("system shutdown", True)]
    for cmd, expected in tests:
        r = router.check_permission(cmd)
        status = "PASS" if r.granted == expected else "FAIL"
        print(f"  [{status}] '{cmd}' -> granted={r.granted} ({r.reason})")

    print("\n[SYSTEM CONTROL]")
    r = router.execute("volume get")
    vol_result = r.get("result", {})
    print(f"  volume get: success={vol_result.get('success')} {vol_result.get('volume', 'N/A')}%")

    print("\n[APP MANAGER - launch notepad]")
    r = router.execute("launch notepad")
    print(f"  result: success={r.get('success')}")

    print("\n[FILE MANAGER - scan sensitive]")
    tmp_dir = Path(os.environ.get("TEMP", str(Path.home()))) / "omega_router_test"
    tmp_dir.mkdir(exist_ok=True)
    test_file = tmp_dir / "router_test.txt"
    test_file.write_text("password=secret123\napi_key=abcdef0123456789", encoding="utf-8")
    r = router.execute(f"scan sensitive {test_file}")
    risk_val = r.get("risk") or r.get("result", {}).get("risk")
    risk_name = str(risk_val) if risk_val else "N/A"
    print(f"  result: risk={risk_name}")

    test_file.unlink(missing_ok=True)
    entries = pm.get_audit_entries(limit=5)
    print(f"\n[AUDIT LOG] ({len(entries)} entries):")
    for e in entries:
        print(f"  #{e[0]} [{e[2]:<9}] {e[3][:50]} -> {e[4]}")

    print("\n" + "=" * 70)
    print("Automation Router self-test complete.")
    print("=" * 70)
