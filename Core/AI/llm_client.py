# LLMClient for C.E.L.E.S.T.E. — Hybrid Local (llama-server.exe) + Cloud API Router
import json
import os
from typing import Any, Dict, Generator, List, Optional

import requests


class LLMClient:
    DEFAULT_BASE_URL = 'http://127.0.0.1:8080/v1'
    MODEL_NAME = 'qwen3.5-9b'
    DEFAULT_TEMPERATURE = 0.4
    DEFAULT_MAX_TOKENS = 1024

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

    @staticmethod
    def _sanitize_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Ensure strict Jinja template compliance for llama-server.exe:
        1. Single 'system' message strictly at index 0.
        2. Strictly alternating 'user' / 'assistant' roles after system.
        3. Final message is always 'user'.
        """
        sys_parts: List[str] = []
        conv: List[Dict[str, str]] = []

        for m in messages:
            role = (m.get('role') or 'user').lower().strip()
            content = (m.get('content') or '').strip()
            if not content:
                continue
            if role == 'system':
                sys_parts.append(content)
            elif role in ('user', 'assistant'):
                if conv and conv[-1]['role'] == role:
                    # Merge consecutive identical roles so Jinja never throws 500
                    conv[-1]['content'] += '\n\n' + content
                else:
                    conv.append({'role': role, 'content': content})

        # Ensure conversation starts with 'user'
        while conv and conv[0]['role'] != 'user':
            conv.pop(0)

        # Ensure conversation ends with 'user'
        if not conv:
            # Fallback: use system content as a user turn so the returned
            # list always starts with at most one 'system' message (index 0),
            # alternates user/assistant, and ends with a 'user' message for
            # llama-server.exe Jinja template compatibility.
            if sys_parts:
                conv.append({'role': 'user', 'content': '\\n\\n'.join(sys_parts)})
            else:
                conv.append({'role': 'user', 'content': 'Hello'})
        elif conv[-1]['role'] != 'user':
            conv.append({'role': 'user', 'content': 'ادامه بده و نتیجه را به فارسی بگو.'})

        out: List[Dict[str, str]] = []
        if sys_parts:
            out.append({'role': 'system', 'content': '\n\n'.join(sys_parts)})
        out.extend(conv)
        return out

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
        ok = self._manager.ensure_model_loaded(timeout_seconds=120)
        if ok:
            self._connection_lost = False
        return ok

    def _build_payload(self, messages: List[Dict[str, str]], stream: bool = False, max_tokens_override: Optional[int] = None) -> str:
        clean_msgs = self._sanitize_messages(messages)
        payload = {
            'messages': clean_msgs,
            'temperature': self.temperature,
            'max_tokens': max_tokens_override or self.max_tokens,
            'stream': stream,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _post(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        url = self.base_url + '/chat/completions'
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        payload_str = self._build_payload(messages, stream=False)
        resp = requests.post(url, data=payload_str.encode('utf-8'), headers=headers, timeout=120)

        # Fallback if history length or template still caused a 500/400 error: send only [system, last_user]
        if resp.status_code >= 400:
            clean = self._sanitize_messages(messages)
            minimal = [clean[0], clean[-1]] if len(clean) >= 2 and clean[0]['role'] == 'system' else [clean[-1]]
            retry_payload = self._build_payload(minimal, stream=False, max_tokens_override=512)
            resp = requests.post(url, data=retry_payload.encode('utf-8'), headers=headers, timeout=120)

        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError('Unexpected response format from LLM API.')
        return body

    # ==================== CLOUD PROVIDERS ====================

    def _call_gemini(self, messages: List[Dict[str, str]], cloud_cfg: Dict[str, Any]) -> Dict[str, Any]:
        api_key = (cloud_cfg.get('gemini_api_key') or os.environ.get('GEMINI_API_KEY') or '').strip()
        if not api_key:
            raise RuntimeError('Gemini API key is not configured.')
        model = cloud_cfg.get('gemini_model', 'gemini-2.5-flash')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'

        clean_msgs = self._sanitize_messages(messages)
        system_instruction = ''
        contents = []
        for m in clean_msgs:
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
            raise RuntimeError('Claude API key is not configured.')
        model = cloud_cfg.get('claude_model', 'claude-3-7-sonnet-latest')
        clean_msgs = self._sanitize_messages(messages)
        system_text = '\n'.join(m.get('content', '') for m in clean_msgs if m.get('role') == 'system').strip()
        chat_msgs = [
            {'role': 'assistant' if m.get('role') == 'assistant' else 'user', 'content': m.get('content', '')}
            for m in clean_msgs if m.get('role') != 'system'
        ]
        payload: Dict[str, Any] = {
            'model': model,
            'max_tokens': self.max_tokens,
            'temperature': self.temperature,
            'messages': chat_msgs,
        }
        if system_text:
            payload['system'] = system_text

        headers = {'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
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
            raise RuntimeError('OpenAI API key is not configured.')
        model = cloud_cfg.get('openai_model', 'gpt-4o')
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        payload = {
            'model': model,
            'messages': self._sanitize_messages(messages),
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
        }
        resp = requests.post('https://api.openai.com/v1/chat/completions', json=payload, headers=headers, proxies=self._get_proxies(cloud_cfg), timeout=35)
        resp.raise_for_status()
        data = resp.json()
        self.last_used_provider = f'openai ({model})'
        data['provider'] = self.last_used_provider
        return data

    # ==================== UNIFIED CHAT ====================

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
                print(f'[LLMClient WARN] Cloud provider {target} unavailable ({exc}); falling back to local.')

        if not self._ensure_model_loaded():
            raise RuntimeError('LLM server is unavailable.')
        self.last_used_provider = 'local'
        return self._post(messages)

    def chat_stream(self, messages: List[Dict[str, str]], provider: Optional[str] = None) -> Generator[str, None, None]:
        resp = self.chat(messages, provider=provider)
        content = resp.get('choices', [{}])[0].get('message', {}).get('content', '')
        if content:
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