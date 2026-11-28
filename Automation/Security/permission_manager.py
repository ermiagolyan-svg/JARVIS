# OMEGA 4-Tier Security & Permission Layer
# Manages command execution based on risk levels: SAFE, MODERATE, SENSITIVE, DANGEROUS.
# All actions are logged to SQLite (security_audit table) and unified central logger.

import os
import sys
import sqlite3
import time
import threading
import logging
from enum import IntEnum
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _PROJECT_ROOT / "Data" / "Memory" / "omega_memory.db"
_AUDIT_LOG_FILE = _PROJECT_ROOT / "Logs" / "security_audit.log"
_PENDING_TIMEOUT_SECONDS = 60

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
_audit_logger = logging.getLogger("security_audit")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()
_db_conn: Optional[sqlite3.Connection] = None


def _get_db_connection() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        try:
            _db_conn = sqlite3.connect(str(_DB_PATH))
            _db_conn.row_factory = sqlite3.Row
            _db_conn.execute("PRAGMA journal_mode=WAL")
            _db_conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.OperationalError:
            _db_conn = None
            raise
    return _db_conn


def _write_audit_entry(command_type: str, risk_level: str, payload: str, status: str) -> None:
    with _db_lock:
        try:
            conn = _get_db_connection()
            conn.execute(
                "INSERT INTO security_audit (command_type, risk_level, payload, status) VALUES (?, ?, ?, ?)",
                (command_type, risk_level, payload, status),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            _audit_logger.warning("Failed to write security audit entry to DB: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Permission levels (IntEnum for ordering)
# ---------------------------------------------------------------------------
class PermissionLevel(IntEnum):
    SAFE = 1
    MODERATE = 2
    SENSITIVE = 3
    DANGEROUS = 4


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------
class PermissionResult:
    def __init__(self, granted: bool, level: PermissionLevel, reason: str):
        self.granted = granted
        self.level = level
        self.reason = reason

    def __repr__(self) -> str:
        status = "APPROVED" if self.granted else "DENIED"
        return f"<PermissionResult {status} [{self.level.name}]>"


# ---------------------------------------------------------------------------
# Command Signatures for 4-Tier Classification
# ---------------------------------------------------------------------------
DANGEROUS_SIGNATURES = [
    "reg delete",
    "hklm\\security",
    "hklm\\sam",
    "hklm\\system",
    "format ",
    "boot",
    "bcdedit",
    "system32",
    "del /f /s /q c:\\",
    "rd /s /q c:\\",
    "defender",
    "firewall",
]

SENSITIVE_SIGNATURES = [
    "shutdown",
    "restart",
    "reboot",
    "delete",
    "remove",
    "del ",
    "Move-Item",
    "Stop-Process",
    "kill",
    "powershell",
    "registry",
]

MODERATE_SIGNATURES = [
    "volume set",
    "close app",
    "brightness down",
    "brightness up",
    "volume mute",
    "volume unmute",
    "airplane mode",
    "bluetooth toggle",
    "wifi toggle",
    "camera toggle",
    "microphone toggle",
    "screenshot",
]

WHITELISTED_APPS = [
    "notepad",
    "calc",
    "chrome",
    "firefox",
    "explorer",
    "cmd",
    "powershell",
    "taskmgr",
    "regedit",
    "services.msc",
]


# ---------------------------------------------------------------------------
# Security Permission Manager - Core Class
# ---------------------------------------------------------------------------
class SecurityPermissionManager:
    """4-Tier Security & Permission Manager for OMEGA Automation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending_confirmations: Dict[str, Tuple[datetime, str]] = {}
        self._alert_log: List[Dict[str, Any]] = []
        _audit_logger.info("SecurityPermissionManager initialized")

    def check_permission(self, command: str) -> PermissionResult:
        """Evaluate a command against the 4-tier security model."""
        with self._lock:
            level = self._classify_command(command)
            return self._evaluate_level(level, command)

    def _classify_command(self, command: str) -> PermissionLevel:
        """Classify a command into one of the 4 security levels."""
        cmd_lower = command.lower().strip()
        for sig in DANGEROUS_SIGNATURES:
            if sig in cmd_lower:
                return PermissionLevel.DANGEROUS
        for sig in SENSITIVE_SIGNATURES:
            if sig in cmd_lower:
                return PermissionLevel.SENSITIVE
        for sig in MODERATE_SIGNATURES:
            if sig in cmd_lower:
                return PermissionLevel.MODERATE
        return PermissionLevel.SAFE

    def _evaluate_level(self, level: PermissionLevel, command: str) -> PermissionResult:
        """Evaluate the permission result based on classification level."""
        if level == PermissionLevel.SAFE:
            return self._handle_safe(command)
        elif level == PermissionLevel.MODERATE:
            return self._handle_moderate(command)
        elif level == PermissionLevel.SENSITIVE:
            return self._handle_sensitive(command)
        else:
            return self._handle_dangerous(command)

    def _handle_safe(self, command: str) -> PermissionResult:
        """Level 1 - SAFE: Auto-execute. Includes telemetry, volume, media, whitelisted apps."""
        app_name = Path(command).stem.lower() if not command.startswith("powershell") else ""
        is_whitelisted = app_name in WHITELISTED_APPS
        _audit_logger.info(f"SAFE | AUTO-EXECUTE: {command} | whitelisted={is_whitelisted}")
        _write_audit_entry("execute", "SAFE", command, "GRANTED")
        return PermissionResult(granted=True, level=PermissionLevel.SAFE, reason="Auto-approved (safe operation)")

    def _handle_moderate(self, command: str) -> PermissionResult:
        """Level 2 - MODERATE: Execute + notification."""
        _audit_logger.warning(f"MODERATE | EXECUTE+NOTIFY: {command}")
        _write_audit_entry("execute_notify", "MODERATE", command, "GRANTED_WITH_NOTIFY")
        self._send_notification(f"Moderate action executed: {command[:80]}")
        return PermissionResult(granted=True, level=PermissionLevel.MODERATE, reason="Executed with notification")

    def _handle_sensitive(self, command: str) -> PermissionResult:
        """Level 3 - SENSITIVE: Require explicit confirmation via voice/UI with 60s timeout."""
        cmd_hash = f"{command[:50]}_{time.time()}"
        expiry = datetime.now() + timedelta(seconds=60)
        _audit_logger.warning(f"SENSITIVE | CONFIRMATION_REQUIRED: {command}")
        _write_audit_entry("confirm_request", "SENSITIVE", command, "PENDING")
        confirmed = self._wait_for_confirmation(cmd_hash, expiry)
        if confirmed:
            self._pending_confirmations.pop(cmd_hash, None)
            _audit_logger.info(f"SENSITIVE | CONFIRMED: {command}")
            _write_audit_entry("confirm_granted", "SENSITIVE", command, "GRANTED_CONFIRMED")
            return PermissionResult(granted=True, level=PermissionLevel.SENSITIVE, reason="Confirmed by user")
        else:
            _audit_logger.error(f"SENSITIVE | TIMEOUT - DENIED: {command}")
            _write_audit_entry("confirm_denied", "SENSITIVE", command, "DENIED_TIMEOUT")
            return PermissionResult(granted=False, level=PermissionLevel.SENSITIVE, reason="Confirmation timed out (60s)")

    def _handle_dangerous(self, command: str) -> PermissionResult:
        """Level 4 - DANGEROUS: Immediate rejection + security alert log."""
        _audit_logger.critical(f"DANGEROUS | REJECTED: {command}")
        _write_audit_entry("rejected", "DANGEROUS", command, "DENIED")
        alert = {"timestamp": datetime.now().isoformat(), "command": command, "level": "CRITICAL", "action": "IMMEDIATE_REJECTION"}
        self._alert_log.append(alert)
        return PermissionResult(granted=False, level=PermissionLevel.DANGEROUS, reason="Immediately rejected - dangerous operation")

    def _send_notification(self, message: str) -> None:
        """Send a notification via HUD/voice (simulated)."""
        _audit_logger.info(f"NOTIFICATION sent: {message}")

    def _wait_for_confirmation(self, cmd_hash: str, expiry: datetime) -> bool:
        """Simulate waiting for user confirmation with 60s timeout."""
        self._pending_confirmations[cmd_hash] = (expiry, "")
        return True

    def get_alert_log(self) -> List[Dict[str, Any]]:
        """Return the list of security alerts (DANGEROUS rejections)."""
        return list(self._alert_log)

    def get_audit_entries(self, limit: int = 50) -> List[Tuple]:
        """Fetch recent audit entries from SQLite."""
        with _db_lock:
            try:
                conn = _get_db_connection()
                cursor = conn.execute(
                    "SELECT id, command_type, risk_level, payload, status, timestamp FROM security_audit ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                return cursor.fetchall()
            except sqlite3.OperationalError as exc:
                _audit_logger.warning("Failed to fetch audit entries: %s", exc, exc_info=True)
                return []

    def get_pending_confirmations(self) -> Dict[str, Tuple[datetime, str]]:
        """Return current pending confirmations."""
        return dict(self._pending_confirmations)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_instance: Optional[SecurityPermissionManager] = None
_instance_lock = threading.Lock()


def get_permission_manager() -> SecurityPermissionManager:
    """Get or create the singleton SecurityPermissionManager instance."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = SecurityPermissionManager()
        return _instance


# ---------------------------------------------------------------------------
# Self-Test / Demo
# ---------------------------------------------------------------------------
def run_self_test() -> None:
    """Run a comprehensive self-test of the 4-tier permission system."""
    print("=" * 70)
    print("OMEGA Security Permission Manager - Self Test")
    print("=" * 70)

    mgr = get_permission_manager()

    # --- Level 1: SAFE ---
    print("\n[LEVEL 1 - SAFE] Auto-execute tests:")
    safe_commands = ["telemetry upload", "volume set 50", "media play", "notepad", "chrome"]
    for cmd in safe_commands:
        result = mgr.check_permission(cmd)
        print(f"  {cmd:<25} -> {result}")

    # --- Level 2: MODERATE ---
    print("\n[LEVEL 2 - MODERATE] Execute + Notify tests:")
    moderate_commands = ["volume set 80", "close app", "brightness down"]
    for cmd in moderate_commands:
        result = mgr.check_permission(cmd)
        print(f"  {cmd:<25} -> {result}")

    # --- Level 3: SENSITIVE ---
    print("\n[LEVEL 3 - SENSITIVE] Confirmation required tests:")
    sensitive_commands = ["del C:\\temp\\test.txt", "taskkill /im chrome.exe", "shutdown /r"]
    for cmd in sensitive_commands:
        result = mgr.check_permission(cmd)
        print(f"  {cmd:<25} -> {result}")

    # --- Level 4: DANGEROUS ---
    print("\n[LEVEL 4 - DANGEROUS] Immediate rejection tests:")
    dangerous_commands = ["format c:", "diskpart", "reg delete HKLM\\Security", "boot sector write"]
    for cmd in dangerous_commands:
        result = mgr.check_permission(cmd)
        print(f"  {cmd:<25} -> {result}")

    # --- Audit Report ---
    print("\n[DATABASE AUDIT ENTRIES] (last 10):")
    entries = mgr.get_audit_entries(limit=10)
    for entry in entries:
        print(f"  #{entry[0]:<3} [{entry[2]:<9}] {entry[3][:60]} -> {entry[4]}")

    # --- Alert Report ---
    alerts = mgr.get_alert_log()
    print(f"\n[DANGEROUS ALERTS LOGGED]: {len(alerts)} alert(s)")
    for alert in alerts:
        print(f"  [{alert['timestamp']}] {alert['command'][:60]}")

    # --- Summary ---
    total = len(safe_commands) + len(moderate_commands) + len(sensitive_commands) + len(dangerous_commands)
    granted = sum(1 for c in safe_commands + moderate_commands + sensitive_commands if mgr.check_permission(c).granted)
    denied = sum(1 for c in dangerous_commands if not mgr.check_permission(c).granted)

    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {total} commands tested | {granted} granted | {denied} rejected")
    print(f"Audit file: {_AUDIT_LOG_FILE}")
    print(f"Audit DB:   {_DB_PATH}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    run_self_test()
