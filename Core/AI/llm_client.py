# LLMClient for C.E.L.E.S.T.E. — Hybrid Local (llama-server.exe Qwen 3.5 9B)
# + Cloud API Router (Google Gemini, Anthropic Claude, OpenAI ChatGPT).
import json
import os
import re
import time
from typing import Any, Dict, Generator, List, Optional

import requests


class LLMClient:
    # Communicates with local llama-server.exe at /v1/chat/completions
    # and routes explicitly requested or complex queries to Gemini, Claude, or ChatGPT.

    DEFAULT_BASE_URL = 'http://localhost:8080/v1'
    MODEL_NAME = 'qwen3.5-9b'
    DEFAULT_TEMPERATURE = 0.4
    DEFAULT_MAX_TOKENS = 2048

    PROVIDER_KEYWORDS = {
        'gemini': ['جمینای', 'جمینی', 'gemini', 'گوگل جمینای'],
        'claude': ['کلود', 'آنتروپیک', 'claude', 'anthropic'],
        'openai': ['چت جی پی تی', 'چت‌جی‌پی‌تی', 'جی پی تی', 'chatgpt', 'gpt-4', 'openai'],
    }

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
            self.model_name = self._config.get('target_model_name', 'qwen3.5-9b')
        self.temperature = self.DEFAULT_TEMPERATURE
        self.max_tokens = self.DEFAULT_MAX_TOKENS
        self._connection_lost = False
        self.last_used_provider = 'local'

    def _load_config(self):
        from Core.Config.config_loader import get_config
        self._config = get_config()
        if not hasattr(self, '_initialized'):
            self._config.load()
            self._initialized = True

    def _get_base_url_from_config(self):
        section = self._config.get_section('llama_cpp') or {}
        host = section.get('host', '127.0.0.1')
        port = section.get('port', 8080)
        return 'http://{}:{}/v1'.format(host, port)

    def _get_cloud_config(self) -> Dict[str, Any]:
        if self._config is None:
            return {}
        self._config.load()
        return self._config.get_section('cloud_ai') or {}

    def _get_proxies(self, cloud_cfg: Dict[str, Any]) -> Optional[Dict[str, str]]:
        proxy = (cloud_cfg.get('proxy_url') or os.environ.get('HTTPS_PROXY') or '').strip()
        if proxy:
            return {'http': proxy, 'https': proxy}
        return None

    def detect_requested_provider(self, messages: List[Dict[str, str]], explicit_provider: Optional[str] = None) -> str:
        if explicit_provider and explicit_provider.lower() in ('local', 'gemini', 'claude', 'openai'):
            return explicit_provider.lower()

        cloud_cfg = self._get_cloud_config()
        if not cloud_cfg.get('enabled', True):
            return 'local'

        last_user_text = ''
        for msg in reversed(messages):
            if msg.get('role') == 'user':
                last_user_text = (msg.get('content') or '').lower()
                break

        for provider, keywords in self.PROVIDER_KEYWORDS.items():
            for kw in keywords:
                if kw in last_user_text:
                    return provider

        return 'local'

    def _ensure_model_loaded(self):
        from Core.ModelManager.llamacpp_manager import LlamaCppManager
        if self._manager is None:
            self._manager = LlamaCppManager()
        health_url = self.base_url.replace('/v1', '') + '/health'
        try:
            resp = requests.get(health_url, timeout=3)
            if resp.status_code == 200 and 'ok' in resp.text.lower():
                self._connection_lost = False
                return True
        except Exception:
            pass
        print('[LLMClient] Server not responding; ensuring model is loaded...')
        ok = self._manager.ensure_model_loaded(timeout_seconds=120)
        if ok:
            self._connection_lost = False
        else:
            print('[LLMClient ERROR] Model failed to load.')
        return ok

    def _build_payload(self, messages, stream=False):
        payload = {
            'model': self.model_name,
            'messages': messages,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'stream': stream,
        }
        return json.dumps(payload)

    def _post(self, payload):
        url = self.base_url + '/chat/completions'
        resp = requests.post(url, data=payload, headers={'Content-Type': 'application/json'}, timeout=120)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError('Unexpected response format from LLM API.')
        return body

    # ==================== CLOUD PROVIDERS (GEMINI / CLAUDE / OPENAI) ====================

    def _call_gemini(self, messages: List[Dict[str, str]], cloud_cfg: Dict[str, Any]) -> Dict[str, Any]:
        api_key = (cloud_cfg.get('gemini_api_key') or os.environ.get('GEMINI_API_KEY') or '').strip()
        if not api_key:
            raise RuntimeError('Gemini API key is not configured in config.json (cloud_ai.gemini_api_key).')
        model = cloud_cfg.get('gemini_model', 'gemini-2.5-flash')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'

        system_instruction = ''
        contents = []
        for m in messages:
            role = m.get('role', 'user')
            text = m.get('content', '')
            if role == 'system':
                system_instruction += text + '\n'
            elif role == 'assistant':
                contents.append({'role': 'model', 'parts': [{'text': text}]})
            else:
                contents.append({'role': 'user', 'parts': [{'text': text}]})

        payload: Dict[str, Any] = {
            'contents': contents,
            'generationConfig': {'temperature': self.temperature, 'maxOutputTokens': self.max_tokens},
        }
        if system_instruction.strip():
            payload['systemInstruction'] = {'parts': [{'text': system_instruction.strip()}]}

        resp = requests.post(url, json=payload, proxies=self._get_proxies(cloud_cfg), timeout=35)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get('candidates', [])
        reply_text = ''
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            reply_text = ''.join(p.get('text', '') for p in parts)
        self.last_used_provider = f'gemini ({model})'
        return {'choices': [{'message': {'role': 'assistant', 'content': reply_text}}], 'provider': self.last_used_provider}

    def _call_claude(self, messages: List[Dict[str, str]], cloud_cfg: Dict[str, Any]) -> Dict[str, Any]:
        api_key = (cloud_cfg.get('claude_api_key') or os.environ.get('ANTHROPIC_API_KEY') or '').strip()
        if not api_key:
            raise RuntimeError('Claude API key is not configured in config.json (cloud_ai.claude_api_key).')
        model = cloud_cfg.get('claude_model', 'claude-3-7-sonnet-latest')
        system_text = '\n'.join(m.get('content', '') for m in messages if m.get('role') == 'system').strip()
        chat_msgs = [
            {'role': 'assistant' if m.get('role') == 'assistant' else 'user', 'content': m.get('content', '')}
            for m in messages if m.get('role') != 'system'
        ]
        payload: Dict[str, Any] = {
            'model': model,
            'max_tokens': self.max_tokens,
            'temperature': self.temperature,
            'messages': chat_msgs,
        }
        if system_text:
            payload['system'] = system_text

        headers = {
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
        resp = requests.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers, proxies=self._get_proxies(cloud_cfg), timeout=35)
        resp.raise_for_status()
        data = resp.json()
        blocks = data.get('content', [])
        reply_text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
        self.last_used_provider = f'claude ({model})'
        return {'choices': [{'message': {'role': 'assistant', 'content': reply_text}}], 'provider': self.last_used_provider}

    def _call_openai(self, messages: List[Dict[str, str]], cloud_cfg: Dict[str, Any]) -> Dict[str, Any]:
        api_key = (cloud_cfg.get('openai_api_key') or os.environ.get('OPENAI_API_KEY') or '').strip()
        if not api_key:
            raise RuntimeError('OpenAI API key is not configured in config.json (cloud_ai.openai_api_key).')
        model = cloud_cfg.get('openai_model', 'gpt-4o')
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        payload = {
            'model': model,
            'messages': messages,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
        }
        resp = requests.post('https://api.openai.com/v1/chat/completions', json=payload, headers=headers, proxies=self._get_proxies(cloud_cfg), timeout=35)
        resp.raise_for_status()
        data = resp.json()
        self.last_used_provider = f'openai ({model})'
        data['provider'] = self.last_used_provider
        return data

    # ==================== UNIFIED CHAT METHODS ====================

    def chat(self, messages: List[Dict[str, str]], provider: Optional[str] = None) -> Dict[str, Any]:
        target = self.detect_requested_provider(messages, provider)
        if target in ('gemini', 'claude', 'openai'):
            cloud_cfg = self._get_cloud_config()
            try:
                if target == 'gemini':
                    return self._call_gemini(messages, cloud_cfg)
                elif target == 'claude':
                    return self._call_claude(messages, cloud_cfg)
                elif target == 'openai':
                    return self._call_openai(messages, cloud_cfg)
            except Exception as exc:
                print(f'[LLMClient WARN] Cloud provider {target} unavailable ({exc}); falling back to local Qwen 3.5 9B.')

        if not self._ensure_model_loaded():
            raise RuntimeError('LLM server is unavailable.')
        self.last_used_provider = 'local'
        payload = self._build_payload(messages, stream=False)
        return self._post(payload)

    def chat_stream(self, messages: List[Dict[str, str]], provider: Optional[str] = None) -> Generator[str, None, None]:
        target = self.detect_requested_provider(messages, provider)
        if target in ('gemini', 'claude', 'openai'):
            try:
                resp = self.chat(messages, provider=target)
                content = resp.get('choices', [{}])[0].get('message', {}).get('content', '')
                if content:
                    yield content
                    return
            except Exception as exc:
                print(f'[LLMClient WARN] Cloud stream fallback to local: {exc}')

        if not self._ensure_model_loaded():
            raise RuntimeError('LLM server is unavailable.')
        self.last_used_provider = 'local'
        url = self.base_url + '/chat/completions'
        payload = json.loads(self._build_payload(messages, stream=True))
        payload['stream'] = True
        resp = requests.post(url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=120, stream=True)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.strip()
            if line.startswith('data: '):
                data_part = line[6:]
                if data_part == '[DONE]':
                    return
                try:
                    obj = json.loads(data_part)
                except (json.JSONDecodeError, ValueError):
                    continue
                choices = obj.get('choices', [])
                if not choices:
                    continue
                delta = choices[0].get('delta', {})
                content = delta.get('content')
                if content and isinstance(content, str) and content.strip():
                    yield content

    def get_health_status(self):
        health_url = self.base_url.replace('/v1', '') + '/health'
        try:
            resp = requests.get(health_url, timeout=3)
            return resp.status_code == 200 and 'ok' in resp.text.lower()
        except Exception:
            return False

    def set_model(self, model_name):
        self.model_name = model_name


if __name__ == '__main__':
    print('=== C.E.L.E.S.T.E. Hybrid LLMClient Self-Test ===')
    client = LLMClient()
    print('[TEST] Base URL:', client.base_url)
    print('[TEST] Local Model:', client.model_name)
    assert client.detect_requested_provider([{'role': 'user', 'content': 'سلست این سوال رو از جمینای بپرس'}]) == 'gemini'
    assert client.detect_requested_provider([{'role': 'user', 'content': 'با کلود کد رو بررسی کن'}]) == 'claude'
    assert client.detect_requested_provider([{'role': 'user', 'content': 'سلام وضعیت سیستم چطوره؟'}]) == 'local'
    print('[OK] Hybrid Provider Detection (Local / Gemini / Claude / OpenAI) verified.')