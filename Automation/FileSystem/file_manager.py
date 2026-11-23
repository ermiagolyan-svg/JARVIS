# V.E.S.P.E.R. File Manager Module
import os, sys, subprocess, logging, re, hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from enum import IntEnum

_logger = logging.getLogger(__name__)

SENSITIVE_PATTERNS: List[Tuple[str, str]] = [
    (r'(?i)(password|passwd|pwd)\s*[=:]\s*\S+', "Password found"),
    (r'(?i)(api_key|apikey|api_secret)\s*[=:]\s*\S+', "API key found"),
    (r'(?i)(private_key|priv_key)\s*[=:]\s*\S+', "Private key found"),
    (r'(?i)(token|secret)\s*[=:]\s*\S{8,}', "Token/secret found"),
    (r'(?i)(aws_access_key_id|aws_secret_access_key)\s*[=:]\s*\S+', "AWS credentials"),
    (r'(?i)(ssh_private_key|rsa_private_key)\s*[=:]\s*\S+', "SSH private key"),
]

SENSITIVE_EXTENSIONS: List[str] = [
    ".pem", ".key", ".p12", ".pfx", ".keystore",
    ".env", ".credentials", ".htpasswd",
]

class FileAction(IntEnum):
    SEARCH = 1; READ = 2; WRITE = 3; DELETE = 4; LIST = 5; SCAN = 6

class SensitiveFileRisk(IntEnum):
    NONE = 0; LOW = 1; MEDIUM = 2; HIGH = 3

class FileManager:
    """File search, navigation, sensitive-file detection, and permission-gated ops."""
    def __init__(self) -> None:
        self._search_cache: Dict[str, List[str]] = {}
        self._operation_log: List[Dict[str, Any]] = []

    def search(self, pattern: str, root_dir: Optional[str] = None,
               max_results: int = 50) -> Dict[str, Any]:
        if not root_dir:
            root_dir = os.environ.get("USERPROFILE", str(Path.home()))
        try:
            results: List[str] = []
            for dirpath, _, filenames in os.walk(root_dir):
                for fn in filenames:
                    if re.search(pattern, fn, re.IGNORECASE):
                        results.append(os.path.join(dirpath, fn))
                        if len(results) >= max_results:
                            return {"success": True, "results": results, "truncated": True}
            return {"success": True, "results": results, "truncated": False}
        except PermissionError as exc:
            _logger.warning("Search permission denied: %s", exc, exc_info=True)
            return {"success": True, "results": results, "error": str(exc)}
        except Exception as exc:
            _logger.error("Search failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def read_file(self, filepath: str) -> Dict[str, Any]:
        try:
            p = Path(filepath).resolve()
            if not p.is_file():
                return {"success": False, "error": f"File not found: {filepath}"}
            content = p.read_text(encoding="utf-8", errors="replace")
            _logger.info("Read file: %s (%d bytes)", filepath, len(content))
            self._operation_log.append({"action": "read", "path": str(p), "size": len(content)})
            return {"success": True, "content": content, "size": len(content)}
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {filepath}"}
        except Exception as exc:
            _logger.error("Read failed (%s): %s", filepath, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def write_file(self, filepath: str, content: str) -> Dict[str, Any]:
        try:
            p = Path(filepath).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            _logger.info("Write file: %s (%d bytes)", filepath, len(content))
            self._operation_log.append({"action": "write", "path": str(p), "size": len(content)})
            return {"success": True, "path": str(p)}
        except Exception as exc:
            _logger.error("Write failed (%s): %s", filepath, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def delete_file(self, filepath: str) -> Dict[str, Any]:
        try:
            p = Path(filepath).resolve()
            if not p.exists():
                return {"success": True, "message": "File already gone"}
            p.unlink()
            _logger.info("Deleted: %s", filepath)
            self._operation_log.append({"action": "delete", "path": str(p)})
            return {"success": True, "deleted": str(p)}
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {filepath}"}
        except Exception as exc:
            _logger.error("Delete failed (%s): %s", filepath, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def list_dir(self, dirpath: str, include_hidden: bool = False) -> Dict[str, Any]:
        try:
            p = Path(dirpath).resolve()
            if not p.is_dir():
                return {"success": False, "error": f"Not a directory: {dirpath}"}
            entries = []
            for entry in sorted(p.iterdir()):
                if not include_hidden and entry.name.startswith("."):
                    continue
                entries.append({"name": entry.name, "is_dir": entry.is_dir(), "size": entry.stat().st_size if entry.is_file() else None})
            return {"success": True, "entries": entries, "total": len(entries)}
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {dirpath}"}
        except Exception as exc:
            _logger.error("List failed (%s): %s", dirpath, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def scan_sensitive(self, filepath: str) -> Dict[str, Any]:
        try:
            p = Path(filepath).resolve()
            if not p.is_file():
                return {"success": False, "error": f"Not a file: {filepath}"}
            ext = p.suffix.lower()
            if ext in SENSITIVE_EXTENSIONS:
                return {"success": True, "risk": SensitiveFileRisk.HIGH, "reason": f"Sensitive extension: {ext}"}
            content = p.read_text(encoding="utf-8", errors="replace")[:50000]
            findings: List[Tuple[str, str]] = []
            for pattern, label in SENSITIVE_PATTERNS:
                matches = re.findall(pattern, content)
                if matches:
                    findings.append((label, f"{len(matches)} match(es)"))
            if not findings:
                return {"success": True, "risk": SensitiveFileRisk.NONE, "findings": []}
            risk = SensitiveFileRisk.HIGH if len(findings) > 2 else (SensitiveFileRisk.MEDIUM if len(findings) > 1 else SensitiveFileRisk.LOW)
            return {"success": True, "risk": risk, "findings": findings}
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {filepath}"}
        except Exception as exc:
            _logger.error("Scan failed (%s): %s", filepath, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def get_operation_log(self) -> List[Dict[str, Any]]:
        return list(self._operation_log)


_instance: Optional[FileManager] = None

def get_file_manager() -> FileManager:
    global _instance
    if _instance is None:
        _instance = FileManager()
    return _instance


if __name__ == "__main__":
    print("=" * 70)
    print("V.E.S.P.E.R. File Manager - Self Test")
    print("=" * 70)
    fm = get_file_manager()
    tmp_dir = Path(os.environ.get("TEMP", str(Path.home()))) / "vesper_test"
    tmp_dir.mkdir(exist_ok=True)
    test_file = tmp_dir / "test_sensitive.txt"
    test_file.write_text("password=secret123\napi_key=abcdef0123456789\nnormal text here", encoding="utf-8")
    print("\n[SENSITIVE SCAN]")
    r = fm.scan_sensitive(str(test_file))
    print(f"  risk={r['risk'].name if isinstance(r.get('risk'), SensitiveFileRisk) else 'ERROR'}")
    for f in r.get("findings", []):
        print(f"  - {f[0]}: {f[1]}")
    print("\n[WRITE TEST]")
    r = fm.write_file(str(tmp_dir / "output.txt"), "Hello V.E.S.P.E.R.!")
    print(f"  success={r['success']}, path={r.get('path')}")
    print("\n[READ TEST]")
    r = fm.read_file(str(tmp_dir / "output.txt"))
    print(f"  success={r['success']}, content='{r.get('content')}'")
    print("\n[DELETE TEST]")
    r = fm.delete_file(str(tmp_dir / "output.txt"))
    print(f"  deleted={r.get('deleted')}")
    test_file.unlink(missing_ok=True)
    print("\n[OPERATION LOG]")
    for entry in fm.get_operation_log():
        print(f"  {entry['action']}: {entry['path']}")
    print("\n" + "=" * 70)
    print("File Manager self-test complete.")
    print("=" * 70)
