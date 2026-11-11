"""
JARVIS Configuration Engine
Thread-safe Singleton ConfigManager that loads, validates, reads, updates,
and saves Configuration/config.json with automatic default restoration.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional


# Absolute path to the project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Absolute path to the configuration file
CONFIG_PATH = PROJECT_ROOT / "Configuration" / "config.json"

# Default configuration values (used for automatic restoration of missing keys)
DEFAULT_CONFIG: Dict[str, Any] = {
    "system": {
        "project_name": "JARVIS",
        "version": "1.0.0",
        "language": "fa",
        "fallback_language": "en",
        "single_instance_port": 8764,
    },
    "llama_cpp": {
        "server_exe_path": "llama-server.exe",
        "model_gguf_path": "D:/AI/Models/qwen3.5-9b.gguf",
        "target_model_name": "Qwen 3.5 9B (llama.cpp CUDA 13.5)",
        "host": "127.0.0.1",
        "port": 8080,
        "base_url": "http://127.0.0.1:8080/v1",
        "health_url": "http://127.0.0.1:8080/health",
        "n_gpu_layers": 99,
        "context_length": 8192,
        "cpu_threads": 8,
        "flash_attn": True,
        "kv_cache_type": "q8_0",
        "temperature": 0.4,
        "timeout_seconds": 60,
        "auto_launch_server": True,
    },
    "voice": {
        "wake_word": "hey_jarvis",
        "threshold": 0.5,
        "stt_model": "small",
        "stt_device": "cpu",
        "stt_compute_type": "int8",
        "tts_model_path": "Data/Models/piper/fa_IR-gyro-medium.onnx",
        "sample_rate": 16000,
        "dsp_pitch_semitones": -2.5,
        "dsp_metallic_mix": 0.22,
        "dsp_reverb_decay": 0.18,
    },
    "network": {
        "host": "0.0.0.0",
        "port": 8765,
        "ws_port": 8766,
        "require_auth": True,
        "token_file": "Configuration/auth_token.json",
        "heartbeat_interval": 10,
    },
    "memory": {
        "db_path": "Data/Memory/jarvis_memory.db",
        "max_context_messages": 20,
        "retention_days": 365,
    },
    "security": {
        "require_confirmation_for_sensitive": True,
        "allow_powershell": True,
        "audit_log_path": "Logs/security_audit.log",
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigManager:
    """
    Thread-safe Singleton configuration manager.

    Loads, validates, reads, updates, and saves the JARVIS configuration
    file (Configuration/config.json) with automatic default restoration
    for any missing or invalid keys.

    Usage:
        config = ConfigManager.get_instance()
        config.load()
        model_name = config.get("llama_cpp", "target_model_name")
        config.set("llama_cpp", "temperature", 0.7)
        config.save()
    """

    _instance: Optional["ConfigManager"] = None
    _lock = threading.Lock()
    _config_lock = threading.RLock()

    def __new__(cls) -> "ConfigManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._config: Dict[str, Any] = {}
        self._config_path: Path = CONFIG_PATH
        self._initialized = True

    @classmethod
    def get_instance(cls) -> "ConfigManager":
        """Get the singleton instance of ConfigManager."""
        return cls()

    def load(self) -> bool:
        """
        Load configuration from the config file.

        If the file does not exist or is invalid JSON, automatically
        restore defaults and save them to disk.

        Returns:
            True if configuration was loaded successfully, False otherwise.
        """
        with self._config_lock:
            if not self._config_path.exists():
                self._config = _deep_merge(DEFAULT_CONFIG.copy(), {})
                self.save()
                return True

            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    raw_config = json.load(f)

                if not isinstance(raw_config, dict):
                    raise ValueError("Root config must be a JSON object")

                # Merge loaded config with defaults (defaults fill missing keys)
                self._config = _deep_merge(DEFAULT_CONFIG.copy(), raw_config)
                return True

            except (json.JSONDecodeError, ValueError) as e:
                # File is corrupted or invalid — restore defaults
                self._config = DEFAULT_CONFIG.copy()
                self.save()
                return True

    def save(self) -> bool:
        """
        Save the current configuration to disk.

        Returns:
            True if saved successfully, False otherwise.
        """
        with self._config_lock:
            try:
                # Ensure parent directory exists
                self._config_path.parent.mkdir(parents=True, exist_ok=True)

                with open(self._config_path, "w", encoding="utf-8") as f:
                    json.dump(self._config, f, indent=4, ensure_ascii=False)
                return True

            except (IOError, OSError) as e:
                return False

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """
        Get a configuration value by section and key.

        Args:
            section: Top-level config section (e.g., "llama_cpp", "voice").
            key: Key within the section.
            default: Fallback value if the key is not found.

        Returns:
            The configuration value or the provided default.
        """
        with self._config_lock:
            section_data = self._config.get(section, {})
            if not isinstance(section_data, dict):
                return default
            return section_data.get(key, default)

    def set(self, section: str, key: str, value: Any) -> bool:
        """
        Set a configuration value by section and key.

        Args:
            section: Top-level config section.
            key: Key within the section.
            value: The value to set.

        Returns:
            True if the value was set successfully.
        """
        with self._config_lock:
            if section not in self._config:
                self._config[section] = {}
            if not isinstance(self._config[section], dict):
                self._config[section] = {}
            self._config[section][key] = value
            return True

    def delete(self, section: str, key: str) -> bool:
        """
        Delete a configuration key.

        Args:
            section: Top-level config section.
            key: Key to delete.

        Returns:
            True if the key was deleted, False if it didn't exist.
        """
        with self._config_lock:
            section_data = self._config.get(section, {})
            if isinstance(section_data, dict) and key in section_data:
                del section_data[key]
                return True
            return False

    def get_section(self, section: str) -> Dict[str, Any]:
        """
        Get an entire configuration section.

        Args:
            section: Top-level config section name.

        Returns:
            A dictionary of the section's key-value pairs.
        """
        with self._config_lock:
            section_data = self._config.get(section, {})
            if not isinstance(section_data, dict):
                return {}
            return dict(section_data)

    def validate(self) -> list:
        """
        Validate the current configuration against defaults.

        Returns:
            A list of validation error messages. Empty list means valid.
        """
        errors: list = []

        with self._config_lock:
            for section_name, section_defaults in DEFAULT_CONFIG.items():
                section_data = self._config.get(section_name, {})
                if not isinstance(section_data, dict):
                    errors.append(f"Section '{section_name}' is not a valid dict")
                    continue
                for key, default_value in section_defaults.items():
                    if key not in section_data:
                        errors.append(
                            f"Missing key '{key}' in section '{section_name}'"
                        )
                    elif type(section_data[key]) != type(default_value):
                        errors.append(
                            f"Type mismatch for '{section_name}.{key}': "
                            f"expected {type(default_value).__name__}, "
                            f"got {type(section_data[key]).__name__}"
                        )

        return errors

    def restore_defaults(self) -> bool:
        """
        Restore all configuration to default values and save.

        Returns:
            True if defaults were restored and saved successfully.
        """
        with self._config_lock:
            self._config = DEFAULT_CONFIG.copy()
            return self.save()

    def reload(self) -> bool:
        """
        Reload configuration from disk (resets to file state).

        Returns:
            True if reloaded successfully.
        """
        with self._config_lock:
            self._config = {}
            return self.load()

    def to_dict(self) -> Dict[str, Any]:
        """
        Get a deep copy of the entire configuration as a dictionary.

        Returns:
            A dictionary copy of the current configuration.
        """
        with self._config_lock:
            return _deep_merge(DEFAULT_CONFIG.copy(), self._config)

    def __repr__(self) -> str:
        with self._config_lock:
            return f"ConfigManager(config_path={self._config_path})"


# Module-level convenience accessor
_config_manager: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    """Get the global ConfigManager singleton instance."""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager.get_instance()
    return _config_manager


def load_config() -> bool:
    """Load configuration from file (convenience function)."""
    return get_config().load()


def save_config() -> bool:
    """Save configuration to file (convenience function)."""
    return get_config().save()