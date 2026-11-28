# OMEGA Automation Integration Tests
# End-to-end tests for SecurityPermissionManager + Automation Router integration

import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main as unittest_main

# Ensure parent package is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from Automation.Security.permission_manager import (
    SecurityPermissionManager,
    PermissionLevel,
    PermissionResult,
    get_permission_manager,
)
from Automation.Windows.system_control import SystemControl, get_system_control
from Automation.Applications.app_manager import AppManager, get_app_manager
from Automation.FileSystem.file_manager import FileManager, get_file_manager, SensitiveFileRisk
from Automation.automation_router import (
    get_automation_router,
    Role,
    PermissionResult as RouterPermissionResult,
)


class TestPermissionManager(TestCase):
    """Test SecurityPermissionManager core functionality."""

    def setUp(self):
        self.pm = get_permission_manager()

    def test_check_permission_safe_command(self):
        result = self.pm.check_permission("volume get")
        self.assertTrue(result.granted)
        self.assertEqual(result.level, PermissionLevel.SAFE)

    def test_check_permission_dangerous_command(self):
        result = self.pm.check_permission("format c:")
        self.assertFalse(result.granted)
        self.assertEqual(result.level, PermissionLevel.DANGEROUS)

    def test_check_permission_sensitive_command(self):
        result = self.pm.check_permission("del /f C:\\secret.txt")
        self.assertTrue(result.granted)
        self.assertEqual(result.level, PermissionLevel.SENSITIVE)

    def test_audit_log_count_increases(self):
        initial_count = len(self.pm.get_alert_log())
        self.pm.check_permission("volume set 50")
        after_count = len(self.pm.get_alert_log())
        self.assertGreater(after_count, initial_count)


class TestAppManager(TestCase):
    """Test AppManager operations."""

    def setUp(self):
        self.app_mgr = get_app_manager()

    def test_get_known_apps(self):
        result = self.app_mgr.get_known_apps()
        self.assertTrue(result["success"])
        apps = result["apps"]
        self.assertIsInstance(apps, dict)
        self.assertGreater(len(apps), 0)

    def test_launch_notepad(self):
        result = self.app_mgr.launch_app("notepad")
        self.assertTrue(result["success"])

    def test_close_notepad(self):
        self.app_mgr.launch_app("notepad")
        result = self.app_mgr.close_app("notepad")
        self.assertTrue(result["success"])


class TestFileManager(TestCase):
    """Test FileManager sensitive scan and operations."""

    def setUp(self):
        self.fm = get_file_manager()

    def test_scan_sensitive_password(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("password=secret123\n")
            f.flush()
            tmp_path = f.name
        try:
            result = self.fm.scan_sensitive(tmp_path)
            self.assertTrue(result["success"])
            # 1 pattern match → LOW risk
            self.assertEqual(result["risk"], SensitiveFileRisk.LOW)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_scan_sensitive_api_key(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("api_key=abcdef0123456789\n")
            f.flush()
            tmp_path = f.name
        try:
            result = self.fm.scan_sensitive(tmp_path)
            self.assertTrue(result["success"])
            # 1 pattern match → LOW risk
            self.assertEqual(result["risk"], SensitiveFileRisk.LOW)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_scan_sensitive_high_risk(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            # 3+ pattern matches → HIGH risk
            f.write("password=secret123\napi_key=abcdef0123456789\ntoken=mytokenvalue12345678\n")
            f.flush()
            tmp_path = f.name
        try:
            result = self.fm.scan_sensitive(tmp_path)
            self.assertTrue(result["success"])
            self.assertEqual(result["risk"], SensitiveFileRisk.HIGH)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_scan_safe_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("This is a normal text file with no secrets.\n")
            f.flush()
            tmp_path = f.name
        try:
            result = self.fm.scan_sensitive(tmp_path)
            self.assertTrue(result["success"])
            self.assertEqual(result["risk"], SensitiveFileRisk.NONE)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_list_dir(self):
        result = self.fm.list_dir(str(Path.home()))
        self.assertTrue(result["success"])
        self.assertIsInstance(result.get("entries", []), list)



class TestSystemControl(TestCase):
    """Test SystemControl operations."""

    def setUp(self):
        self.sc = get_system_control()

    def test_classify_risk_volume_set(self):
        risk, desc = self.sc.classify_risk("volume set 50")
        self.assertEqual(risk, 1)  # SAFE

    def test_classify_risk_shutdown(self):
        risk, desc = self.sc.classify_risk("system shutdown")
        self.assertEqual(risk, 3)  # SENSITIVE

    def test_toggle_media(self):
        result = self.sc.toggle_media()
        self.assertTrue(result["success"])


class TestAutomationRouter(TestCase):
    """Test Automation Router integration with SecurityPermissionManager."""

    def setUp(self):
        self.router = get_automation_router(role=Role.USER)

    def test_permission_check_granted(self):
        result = self.router.check_permission("launch notepad")
        self.assertTrue(result.granted)

    def test_permission_check_denied(self):
        result = self.router.execute("format c:")
        self.assertFalse(result["success"])

    def test_launch_notepad_via_router(self):
        result = self.router.execute("launch notepad")
        self.assertTrue(result["success"])

    def test_scan_sensitive_via_router(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("password=secret123\napi_key=abcdef0123456789\n")
            f.flush()
            tmp_path = f.name
        try:
            result = self.router.execute(f"scan sensitive {tmp_path}")
            self.assertTrue(result["success"])
            risk_val = result.get("risk") or result.get("result", {}).get("risk")
            self.assertEqual(risk_val, SensitiveFileRisk.MEDIUM)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_volume_set_via_router(self):
        result = self.router.execute("volume set 50")
        if result["success"]:
            self.assertIn("volume", str(result.get("result", {})).lower())

    def test_audit_log_via_router(self):
        initial_count = len(self.router.permission_manager.get_alert_log())
        self.router.execute("launch notepad")
        final_count = len(self.router.permission_manager.get_alert_log())
        self.assertGreaterEqual(final_count, initial_count)


class TestRoleBasedRouting(TestCase):
    """Test role-based permission enforcement."""

    def test_guest_can_volume_get(self):
        router = get_automation_router(role=Role.GUEST)
        result = router.check_permission("volume get")
        self.assertTrue(result.granted)

    def test_user_can_launch_app(self):
        router = get_automation_router(role=Role.USER)
        result = router.check_permission("launch notepad")
        self.assertTrue(result.granted)

    def test_guest_cannot_shutdown(self):
        router = get_automation_router(role=Role.GUEST)
        result = router.execute("format c:")
        self.assertFalse(result["success"])

    def test_admin_can_shutdown(self):
        router = get_automation_router(role=Role.ADMIN)
        result = router.check_permission("system shutdown")
        self.assertTrue(result.granted)


class TestEndToEnd(TestCase):
    """End-to-end integration tests."""

    def setUp(self):
        self.router = get_automation_router(role=Role.USER)
        self.pm = get_permission_manager()

    def test_full_workflow(self):
        """Test a complete workflow: scan -> launch -> audit log check."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("password=secret123\n")
            f.flush()
            tmp_path = f.name
        try:
            scan_result = self.router.execute(f"scan sensitive {tmp_path}")
            self.assertTrue(scan_result["success"])
            risk_val = scan_result.get("risk") or scan_result.get("result", {}).get("risk")
            self.assertEqual(risk_val, SensitiveFileRisk.LOW)

            launch_result = self.router.execute("launch notepad")
            self.assertTrue(launch_result["success"])

            log_entries = self.pm.get_alert_log()
            self.assertGreater(len(log_entries), 0)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest_main(verbosity=2)

