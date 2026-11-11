# LLMClient for JARVIS — communicates with llama-server.exe via OpenAI-compatible API.
import json
import time
from typing import Any, Dict, Generator, List, Optional

import requests


class LLMClient:
    """Communicates with llama-server.exe OpenAI-compatible API at /chat/completions.

    Reads base_url from ConfigManager (defaults to http://localhost:8080/v1).
    Supports standard and streaming responses via the requests library.
    Automatically restarts the model if the server becomes unavailable.
    """

    DEFAULT_BASE_URL = "http://localhost:8080/v1"
    MODEL_NAME = "qwen3.5-9b"
    DEFAULT_TEMPERATURE = 0.4
    DEFAULT_MAX_TOKENS = 2048

    def __init__(self, base_url=None, model_name=None):
        self._manager = None
        self._config = None
        self._load_config()
        if base_url is not None:
            self.base_url = base_url
        else:
            self.base_url = self._get_base_url_from_config()
        if model_name is not None:
            self.model_name = model_name
        else:
            self.model_name = self._config.get("target_model_name", "qwen3.5-9b")
        self.temperature = self.DEFAULT_TEMPERATURE
        self.max_tokens = self.DEFAULT_MAX_TOKENS
        self._connection_lost = False

    def _load_config(self):
        from Core.Config.config_loader import get_config
        self._config = get_config()
        if not hasattr(self, "_initialized"):
            self._config.load()
            self._initialized = True

    def _get_base_url_from_config(self):
        section = self._config.get_section("llama_cpp") or {}
        host = section.get("host", "127.0.0.1")
        port = section.get("port", 8080)
        return "http://{}:{}/v1".format(host, port)

    def _ensure_model_loaded(self):
        from Core.ModelManager.llamacpp_manager import LlamaCppManager
        if self._manager is None:
            self._manager = LlamaCppManager()
        health_url = self.base_url.replace("/v1", "") + "/health"
        try:
            resp = requests.get(health_url, timeout=3)
            if resp.status_code == 200 and "ok" in resp.text.lower():
                self._connection_lost = False
                return True
        except Exception:
            pass
        print("[LLMClient] Server not responding; ensuring model is loaded...")
        ok = self._manager.ensure_model_loaded(timeout_seconds=120)
        if ok:
            self._connection_lost = False
        else:
            print("[LLMClient ERROR] Model failed to load.")
        return ok

    def _build_payload(self, messages, stream=False):
        payload = {"model": self.model_name, "messages": messages, "temperature": self.temperature, "max_tokens": self.max_tokens, "stream": stream}
        return json.dumps(payload)

    def _post(self, payload):
        url = self.base_url + "/chat/completions"
        resp = requests.post(url, data=payload, headers={"Content-Type": "application/json"}, timeout=120)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError("Unexpected response format from LLM API.")
        return body

    def chat(self, messages):
        if not self._ensure_model_loaded():
            raise RuntimeError("LLM server is unavailable.")
        payload = self._build_payload(messages, stream=False)
        return self._post(payload)

    def chat_stream(self, messages):
        if not self._ensure_model_loaded():
            raise RuntimeError("LLM server is unavailable.")
        url = self.base_url + "/chat/completions"
        payload = json.loads(self._build_payload(messages, stream=True))
        payload["stream"] = True
        resp = requests.post(url, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=120, stream=True)
        resp.raise_for_status()
        buffer = ""
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.strip()
            if line.startswith("data: "):
                data_part = line[6:]
                if data_part == "[DONE]":
                    return
                try:
                    obj = json.loads(data_part)
                except (json.JSONDecodeError, ValueError):
                    continue
                choices = obj.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content and isinstance(content, str) and content.strip():
                    yield content

    def get_health_status(self):
        health_url = self.base_url.replace("/v1", "") + "/health"
        try:
            resp = requests.get(health_url, timeout=3)
            return resp.status_code == 200 and "ok" in resp.text.lower()
        except Exception:
            return False

    def set_model(self, model_name):
        self.model_name = model_name


if __name__ == "__main__":
    print("=== LLMClient Self-Test ===")
    client = LLMClient()
    print("[TEST] Base URL:", client.base_url)
    print("[TEST] Model name:", client.model_name)
    health_ok = client.get_health_status()
    print("[TEST] Server healthy:", health_ok)
    if not health_ok:
        print("[LLMClient WARN] No server running. Attempting to start one...")
        from Core.ModelManager.llamacpp_manager import LlamaCppManager
        mgr = LlamaCppManager()
        started = mgr.start_server()
        if started and client.get_health_status():
            health_ok = True
    if health_ok:
        messages = [{"role": "system", "content": "You are a helpful assistant. Respond in Persian."}, {"role": "user", "content": "سلام! خودت را معرفی کن."}]
        print("\n--- Standard chat (first 200 chars) ---")
        resp = client.chat(messages)
        content = resp["choices"][0]["message"]["content"] if resp.get("choices") else ""
        print("Response:", content[:200])
        print("\n--- Streaming chat (first 5 chunks) ---")
        count = 0
        for chunk in client.chat_stream(messages):
            print(chunk, end="", flush=True)
            count += 1
            if count >= 5:
                break
        print()
    else:
        print("[LLMClient WARN] Could not connect to server. Self-test skipped.")
    try:
        LlamaCppManager().stop_managed_server()
    except Exception:
        pass
    print("\n=== Self-test complete ===")
