# LLMClient for JARVIS — communicates with llama-server.exe via OpenAI-compatible API.
import json
import sys
import time
from typing import Any, Dict, Generator, List, Optional

sys.path.insert(0, __file__.replace("\\Core\\AI\\llm_client.py", "").rstrip("\\"))


class LLMClient:
    """Communicates with llama-server.exe's OpenAI-compatible API at http://127.0.0.1:8080/v1/chat/completions."""

    BASE_URL = "http://127.0.0_1:8080"  # placeholder, overridden by config in __init__
    MODEL_NAME = "qwen3.5-9b"
    DEFAULT_TEMPERATURE = 0.4
    DEFAULT_MAX_TOKENS = 2048

    def __init__(self, base_url=None, model_name=None):
        from Core.ModelManager.llamacpp_manager import LlamaCppManager
        self._manager = LlamaCppManager()
        if base_url is not None and model_name is not None:
            self.base_url = base_url
            self.model_name = model_name
        else:
            host = self._manager.host
            port = self._manager.port
            self.base_url = "http://{}:{}/v1".format(host, port)
            self.model_name = self._manager._config.get("target_model_name", "qwen3.5-9b")
        self.temperature: float = self.DEFAULT_TEMPERATURE
        self.max_tokens: int = self.DEFAULT_MAX_TOKENS
        self._connection_lost = False

    def _ensure_server(self):
        """Auto-call LlamaCppManager.ensure_model_loaded() if connection appears lost."""
        import urllib.request as ur
        health_url = "http://{}/health".format(
            self.base_url.replace("/v1", ""),
        )
        try:
            req = ur.Request(health_url, method="GET")
            with ur.urlopen(req, timeout=3) as resp:
                data = resp.read().decode("utf-8", errors="replace").strip()
                if "ok" in data.lower():
                    self._connection_lost = False
                    return True
        except Exception:
            pass

        # Connection lost or never connected — ensure model loaded
        print("[LLMClient] Server not responding; ensuring model is loaded...")
        ok = self._manager.ensure_model_loaded(timeout_seconds=120)
        if ok:
            self._connection_lost = False
        else:
            print("[LLMClient ERROR] Model failed to load.")
        return ok

    def _build_payload(self, messages, stream=False):
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        return json.dumps(payload).encode("utf-8")

    def _post(self, payload):
        """Send a POST to the chat/completions endpoint and return parsed JSON."""
        import urllib.request as ur
        url = "{}/chat/completions".format(self.base_url)
        req = ur.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        resp = ur.urlopen(req, timeout=120)
        body = json.loads(resp.read().decode("utf-8"))
        if not isinstance(body, dict):
            raise RuntimeError("Unexpected response format from LLM API.")
        return body

    def chat(self, messages):
        """Standard (non-streaming) chat completion. Returns the full API JSON response."""
        if not self._ensure_server():
            raise RuntimeError("LLM server is unavailable.")
        payload = self._build_payload(messages, stream=False)
        return self._post(payload)

    def chat_stream(self, messages):
        """Streaming chat completion. Yields delta text chunks (SSE format from llama.cpp)."""
        if not self._ensure_server():
            raise RuntimeError("LLM server is unavailable.")
        payload = self._build_payload(messages, stream=True)
        import urllib.request as ur
        url = "{}/chat/completions".format(self.base_url)
        req = ur.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        resp = ur.urlopen(req, timeout=120)

        buffer = ""
        while True:
            chunk = resp.read(4096).decode("utf-8", errors="replace")
            if not chunk:
                break
            buffer += chunk
            # Parse SSE lines from the buffer
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line.startswith("data: "):
                    data_part = line[6:]
                    if data_part == "[DONE]":
                        return
                    try:
                        obj = json.loads(data_part)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    # OpenAI-compatible SSE chunk structure
                    choices = obj.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content and isinstance(content, str) and content.strip():
                        yield content

    def set_model(self, model_name):
        """Change the target model name (useful for multi-model setups)."""
        self.model_name = model_name


if __name__ == "__main__":
    # Self-test block for llm_client.py (requires running llama-server)
    print("=== LLMClient Self-Test ===")
    client = LLMClient()

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
        messages = [
            {"role": "system", "content": "You are a helpful assistant. Respond in Persian."},
            {"role": "user", "content": "سلام! خودت را معرفی کن."},
        ]

        print("\n--- Standard chat ---")
        resp = client.chat(messages)
        content = resp["choices"][0]["message"]["content"] if resp.get("choices") else ""
        print("Response:", content[:300])

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

    # Cleanup managed server (only if we started one in this script)
    import sys as _sys
    if hasattr(sys, "_jarvis_test_server_started"):
        LlamaCppManager().stop_managed_server()

    print("\n=== Self-test complete ===")

    def get_health_status(self):
        """Check if the llama-server is healthy."""
        import urllib.request as ur
        health_url = "http://{}/health".format(
            self.base_url.replace("/v1", ""),
        )
        try:
            req = ur.Request(health_url, method="GET")
            with ur.urlopen(req, timeout=3) as resp:
                data = resp.read().decode("utf-8", errors="replace").strip()
                return "ok" in data.lower()
        except Exception:
            return False
