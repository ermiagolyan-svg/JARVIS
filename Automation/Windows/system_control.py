# JARVIS Windows System Control Module
import os, sys, subprocess, logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from enum import IntEnum

_logger = logging.getLogger(__name__)

WHITELISTED_PS: List[str] = [
    "Get-Process", "Get-Service", "Get-WmiObject", "Get-CimInstance",
    "Get-ChildItem", "Test-Connection", "Get-HotFix", "Get-WindowsFeature",
    "Get-ComputerInfo", "Get-Process -Name", "Get-Service -Name",
    "Get-CimInstance -ClassName", "Get-WmiObject -Class",
]

class SystemControlRisk(IntEnum):
    SAFE = 1; MODERATE = 2; SENSITIVE = 3; DANGEROUS = 4

class SystemControl:
    """Master control for Windows volume, media keys, and PowerShell."""
    def __init__(self) -> None:
        self._volume_range = (0, 100)
        self._whitelist = WHITELISTED_PS

    def _audio_api(self):
        import comtypes
        from comtypes import CLSCTX_ALL, GUID
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        # Try pycaw pattern: AudioUtilities.GetSpeakers()
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            if hasattr(devices, 'EndpointVolume'):
                return devices.EndpointVolume
            else:
                return devices.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        except Exception as exc:
            _logger.debug("Primary pycaw AudioUtilities path failed: %s", exc)
        # Fallback: older pycaw pattern: devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        try:
            from comtypes.gen import devices
            ep = devices.Activate(GUID(IID_ENUM), CLSCTX_ALL, None)
            return ep.QueryInterface(comtypes.GUID(IID_VOL))
        except (ImportError, AttributeError, NameError) as exc:
            raise RuntimeError(f"Audio API init failed: {exc}")

    def set_volume(self, level: int) -> Dict[str, Any]:
        level = max(0, min(100, level))
        try:
            vol = self._audio_api()
            vol.SetMasterVolumeLevelScalar(level / 100.0, None)
            _logger.info("Volume set to %d%%", level)
            return {"success": True, "volume": level}
        except Exception as exc:
            _logger.error("Volume control failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def get_volume(self) -> Dict[str, Any]:
        try:
            vol = self._audio_api()
            cur = vol.GetMasterVolumeLevelScalar()
            return {"success": True, "volume": int(cur * 100)}
        except Exception as exc:
            _logger.error("Get volume failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def mute_volume(self) -> Dict[str, Any]:
        try:
            vol = self._audio_api()
            muted = vol.GetMute()
            vol.SetMute(not bool(muted), None)
            _logger.info("Mute toggled: %s", not bool(muted))
            return {"success": True, "muted": not bool(muted)}
        except Exception as exc:
            _logger.error("Mute failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _media_key(self, vk_code: int, label: str) -> Dict[str, Any]:
        try:
            import win32api, win32con
            win32api.keybd_event(vk_code, 0, 0, 0)
            win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
            return {"success": True, "action": label}
        except ImportError:
            return {"success": False, "error": "pywin32 not installed"}
        except Exception as exc:
            _logger.warning("Media key %s failed: %s", label, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def media_play_pause(self) -> Dict[str, Any]:
        import win32con
        return self._media_key(win32con.VK_MEDIA_PLAY_PAUSE, "play_pause")

    def toggle_media(self) -> Dict[str, Any]:
        """Alias for media_play_pause (used by router)."""
        return self.media_play_pause()

    def media_next(self) -> Dict[str, Any]:
        import win32con
        return self._media_key(win32con.VK_MEDIA_NEXT_TRACK, "next_track")

    def media_previous(self) -> Dict[str, Any]:
        import win32con
        return self._media_key(win32con.VK_MEDIA_PREV_TRACK, "prev_track")

    def media_stop(self) -> Dict[str, Any]:
        import win32con
        return self._media_key(win32con.VK_MEDIA_STOP, "stop")

    def execute_whitelisted_ps(self, command: str) -> Dict[str, Any]:
        cmd_lower = command.strip().lower()
        is_wl = any(wc.lower() in cmd_lower for wc in self._whitelist)
        if not is_wl:
            _logger.warning("Blocked non-whitelisted PS cmd: %s", command)
            return {"success": False, "error": "Command not whitelisted"}
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=30,
            )
            out = result.stdout.strip() if result.stdout else ""
            err = result.stderr.strip() if result.stderr else ""
            if result.returncode == 0:
                _logger.info("PS OK: %s", command[:60])
                return {"success": True, "output": out}
            _logger.warning("PS err: %s | %s", command[:60], err[:120])
            return {"success": False, "error": err[:500]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command timed out"}
        except Exception as exc:
            _logger.error("PS exec failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def get_whitelisted_commands(self) -> List[str]:
        return list(self._whitelist)

    def classify_risk(self, command: str) -> Tuple[SystemControlRisk, str]:
        c = command.strip().lower()
        if any(kw in c for kw in ["volume set", "volume get", "mute"]):
            return SystemControlRisk.SAFE, "Volume control"
        if any(kw in c for kw in ["media play", "media next", "media prev", "media stop"]):
            return SystemControlRisk.SAFE, "Media key"
        if "powershell" in c or "ps1" in c:
            return SystemControlRisk.MODERATE, "PowerShell execution"
        if any(kw in c for kw in ["shutdown", "restart", "logoff"]):
            return SystemControlRisk.SENSITIVE, "System power command"
        if any(kw in c for kw in ["format", "diskpart", "reg delete"]):
            return SystemControlRisk.DANGEROUS, "Dangerous operation"
        return SystemControlRisk.MODERATE, "Unknown system control"


_instance: Optional[SystemControl] = None

def get_system_control() -> SystemControl:
    global _instance
    if _instance is None:
        _instance = SystemControl()
    return _instance


if __name__ == "__main__":
    print("=" * 70)
    print("JARVIS Windows System Control - Self Test")
    print("=" * 70)
    sc = get_system_control()
    print("\n[RISK CLASSIFICATION]")
    for cmd in ["volume set 50", "media play", "powershell Get-Process", "shutdown /r", "format c:"]:
        risk, desc = sc.classify_risk(cmd)
        print(f"  {cmd:<30} -> {risk.name:<12} ({desc})")
    print(f"\n[WHITELISTED PS COMMANDS] ({len(sc.get_whitelisted_commands())} entries)")
    for wc in sc.get_whitelisted_commands()[:5]:
        print(f"  - {wc}")
    print("\n[WHITELISTED PS EXECUTION]")
    r = sc.execute_whitelisted_ps("Get-Process -Name powershell | Select-Object -First 3")
    print(f"  success={r['success']}, output_len={len(r.get('output',''))}")
    print("\n[BLOCKED COMMAND]")
    r = sc.execute_whitelisted_ps("Invoke-Expression { Remove-Item -Recurse C:\\* }")
    print(f"  blocked={not r['success']}")
    print("\n" + "=" * 70)
    print("System Control self-test complete.")
    print("=" * 70)
