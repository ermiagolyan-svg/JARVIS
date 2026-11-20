# LLMClient for C.E.L.E.S.T.E. — Hybrid Local (llama-server.exe) + Cloud API Router
# Unified Observability: TTFT / Eval / tok/s profiling, explicit fallback logs, turn_id propagation.
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from Core.Logger.logger import get_logger, setup_unified_logging

setup_unified_logging()
logger = get_logger('llm_client')


class LLMClient:
    DEFAULT_BASE_URL = 'http://127.0.0.1:8080/v1'
    MODEL_NAME = 'Gemma 4 26B-A4B IT (IQ4_XS)'
    DEFAULT_TEMPERATURE = 0.6
    DEFAULT_TOP_P = 0.95
    DEFAULT_MAX_TOKENS = 2048

    _THINK_CLOSED_RE = re.compile(r'<(?:think|thought|\|thought\|)>.*?</(?:think|thought|\|thought\|)>', re.DOTALL | re.IGNORECASE)
    _THINK_ORPHAN_RE = re.compile(r'^.*?</(?:think|thought|\|thought\|)>', re.DOTALL | re.IGNORECASE)
    _THINK_UNCLOSED_RE = re.compile(r'<(?:think|thought|\|thought\|)>.*$', re.DOTALL | re.IGNORECASE)

    PROVIDER_KEYWORDS = {
        'gemini': ['جمینای', 'جمینی', 'gemini', 'گوگل جمینای'],
        'claude': ['کلود', 'آنتروپیک', 'claude', 'anthropic'],
        'openai': ['چت جی پی تی', 'چت‌جی‌پی‌تی', 'جی پی تی', 'chatgpt', 'gpt-4', 'openai'],
    }

    def __init__(self, base_url=None, model_name=None):
        self._manager = None
        self._config = None
        self._load_config()
        llama_cfg = self._get_llama_config()

        if base_url is not None:
            self.base_url = base_url
        else:
            self.base_url = self._get_base_url_from_config()

        if model_name is not None:
            self.model_name = model_name
        else:
            self.model_name = llama_cfg.get('target_model_name', self.MODEL_NAME)

        self.temperature = float(llama_cfg.get('temperature', self.DEFAULT_TEMPERATURE))
        self.top_p = float(llama_cfg.get('top_p', self.DEFAULT_TOP_P))
        self.max_tokens = int(llama_cfg.get('max_tokens', self.DEFAULT_MAX_TOKENS))
        self.enable_thinking = bool(llama_cfg.get('enable_thinking', False))
        self._connection_lost = False
        self.last_used_provider = 'local'
        self.last_timings: Dict[str, Any] = {}

    def _load_config(self):
        from Core.Config.config_loader import get_config
        self._config = get_config()
        if not hasattr(self, '_initialized'):
            self._config.load()
            self._initialized = True

    def _get_llama_config(self) -> Dict[str, Any]:
        if self._config is None:
            return {}
        try:
            return self._config.get_section('llama_cpp') or {}
        except Exception:
            return {}

    def _get_base_url_from_config(self):
        section = self._get_llama_config()
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

    @classmethod
    def _strip_thinking_preview(cls, text: str) -> str:
        if not text:
            return ''
        cleaned = cls._THINK_CLOSED_RE.sub('', text)
        cleaned = cls._THINK_ORPHAN_RE.sub('', cleaned)
        cleaned = cls._THINK_UNCLOSED_RE.sub('', cleaned)
        return cleaned.strip()

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
                    conv[-1]['content'] += '\n\n' + content
                else:
                    conv.append({'role': role, 'content': content})

        while conv and conv[0]['role'] != 'user':
            conv.pop(0)

        if not conv:
            if sys_parts:
                conv.append({'role': 'user', 'content': '\n\n'.join(sys_parts)})
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
        logger.warning('Local llama-server /health did not respond OK; waiting for model load...')
        ok = self._manager.ensure_model_loaded(timeout_seconds=120)
        if ok:
            self._connection_lost = False
        return ok

    def _build_payload(
        self,
        messages: List[Dict[str, str]],
        stream: bool = False,
        max_tokens_override: Optional[int] = None,
        enable_thinking_override: Optional[bool] = None,
        include_template_kwargs: bool = True,
    ) -> str:
        clean_msgs = self._sanitize_messages(messages)
        eff_thinking = self.enable_thinking if enable_thinking_override is None else bool(enable_thinking_override)
        payload: Dict[str, Any] = {
            'messages': clean_msgs,
            'temperature': self.temperature,
            'top_p': self.top_p,
            'max_tokens': int(max_tokens_override or self.max_tokens),
            'stream': stream,
        }
        if include_template_kwargs and not eff_thinking:
            payload['chat_template_kwargs'] = {'enable_thinking': False}
        return json.dumps(payload, ensure_ascii=False)

    def _send_local_request(
        self,
        messages: List[Dict[str, str]],
        max_tokens_override: Optional[int] = None,
        enable_thinking_override: Optional[bool] = None,
    ) -> Dict[str, Any]:
        url = self.base_url + '/chat/completions'
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        t0 = time.perf_counter()

        payload_str = self._build_payload(
            messages,
            stream=False,
            max_tokens_override=max_tokens_override,
            enable_thinking_override=enable_thinking_override,
            include_template_kwargs=True,
        )
        resp = requests.post(url, data=payload_str.encode('utf-8'), headers=headers, timeout=120)

        # Fallback 1: retry without chat_template_kwargs if server rejected schema
        if resp.status_code >= 400:
            logger.warning(
                'llama-server HTTP %d on primary payload (%s); retrying without chat_template_kwargs',
                resp.status_code,
                resp.text[:200].replace('\n', ' '),
            )
            payload_fallback = self._build_payload(
                messages,
                stream=False,
                max_tokens_override=max_tokens_override,
                enable_thinking_override=enable_thinking_override,
                include_template_kwargs=False,
            )
            resp = requests.post(url, data=payload_fallback.encode('utf-8'), headers=headers, timeout=120)

        # Fallback 2: minimal [system, last_user] if long history broke context/template
        if resp.status_code >= 400:
            logger.warning(
                'llama-server HTTP %d on fallback-1 (%s); retrying with minimal [system, last_user]',
                resp.status_code,
                resp.text[:200].replace('\n', ' '),
            )
            clean = self._sanitize_messages(messages)
            minimal = [clean[0], clean[-1]] if len(clean) >= 2 and clean[0]['role'] == 'system' else [clean[-1]]
            retry_payload = self._build_payload(
                minimal,
                stream=False,
                max_tokens_override=max_tokens_override or 1024,
                enable_thinking_override=False,
                include_template_kwargs=True,
            )
            resp = requests.post(url, data=retry_payload.encode('utf-8'), headers=headers, timeout=120)

        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError('Unexpected response format from LLM API.')

        wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        timings = body.get('timings') or {}
        usage = body.get('usage') or {}
        choices = body.get('choices') or []
        finish_reason = choices[0].get('finish_reason', '?') if choices else 'none'

        ttft_ms = round(float(timings.get('prompt_ms', 0.0) or 0.0), 1)
        eval_ms = round(float(timings.get('predicted_ms', 0.0) or 0.0), 1)
        tok_s = round(float(timings.get('predicted_per_second', 0.0) or 0.0), 1)
        prompt_n = int(timings.get('prompt_n', usage.get('prompt_tokens', 0)) or 0)
        eval_n = int(timings.get('predicted_n', usage.get('completion_tokens', 0)) or 0)

        self.last_timings = {
            'provider': 'local',
            'model': self.model_name,
            'wall_ms': wall_ms,
            'ttft_ms': ttft_ms,
            'eval_ms': eval_ms,
            'tok_per_s': tok_s,
            'prompt_tokens': prompt_n,
            'completion_tokens': eval_n,
            'finish_reason': finish_reason,
        }

        logger.info(
            'LLM local OK | wall=%.1fms (ttft=%.1fms eval=%.1fms %.1f tok/s) | tokens=%d->%d | finish=%s',
            wall_ms, ttft_ms, eval_ms, tok_s, prompt_n, eval_n, finish_reason,
        )
        return body

    def _post(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        body = self._send_local_request(messages)

        choices = body.get('choices') or []
        if choices:
            msg = choices[0].get('message') or {}
            raw_content = msg.get('content') or ''
            reasoning_content = (msg.get('reasoning_content') or '').strip()
            visible_content = self._strip_thinking_preview(raw_content)

            needs_retry = (not visible_content) and (
                bool(reasoning_content)
                or '<think' in raw_content.lower()
                or '<thought' in raw_content.lower()
                or '<|thought|' in raw_content.lower()
                or choices[0].get('finish_reason') == 'length'
            )

            if needs_retry:
                logger.warning(
                    'Empty visible LLM content detected (reasoning_len=%d, raw_len=%d, finish=%s). Retrying once with enable_thinking=False and max_tokens=%d.',
                    len(reasoning_content),
                    len(raw_content),
                    choices[0].get('finish_reason'),
                    max(self.max_tokens, 2048),
                )
                retry_msgs = [dict(m) for m in messages]
                retry_msgs.insert(0, {
                    'role': 'system',
                    'content': 'بدون تفکر طولانی و بدون تگ <think>، مستقیماً پاسخ نهایی کوتاه و روان را به فارسی بنویس.',
                })
                retry_body = self._send_local_request(
                    retry_msgs,
                    max_tokens_override=max(self.max_tokens, 2048),
                    enable_thinking_override=False,
                )
                retry_choices = retry_body.get('choices') or []
                if retry_choices:
                    retry_msg = retry_choices[0].get('message') or {}
                    retry_raw = retry_msg.get('content') or ''
                    if self._strip_thinking_preview(retry_raw):
                        body = retry_body
                    else:
                        logger.error('Retry still returned empty visible content from llama-server.')
                        retry_msg['content'] = ''
                        body = retry_body

            final_choices = body.get('choices') or []
            if final_choices and isinstance(final_choices[0].get('message'), dict):
                if final_choices[0]['message'].get('content') is None:
                    final_choices[0]['message']['content'] = ''

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

        t0 = time.perf_counter()
        resp = requests.post(url, json=payload, proxies=self._get_proxies(cloud_cfg), timeout=35)
        resp.raise_for_status()
        data = resp.json()
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        candidates = data.get('candidates', [])
        reply_text = ''
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            reply_text = ''.join(p.get('text', '') for p in parts)

        self.last_used_provider = f'gemini ({model})'
        self.last_timings = {'provider': self.last_used_provider, 'wall_ms': wall_ms, 'ttft_ms': wall_ms, 'eval_ms': 0.0, 'tok_per_s': 0.0}
        logger.info('LLM cloud OK | provider=%s | wall=%.1fms | reply_chars=%d', self.last_used_provider, wall_ms, len(reply_text))
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
        t0 = time.perf_counter()
        resp = requests.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers, proxies=self._get_proxies(cloud_cfg), timeout=35)
        resp.raise_for_status()
        data = resp.json()
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        blocks = data.get('content', [])
        reply_text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
        self.last_used_provider = f'claude ({model})'
        self.last_timings = {'provider': self.last_used_provider, 'wall_ms': wall_ms, 'ttft_ms': wall_ms, 'eval_ms': 0.0, 'tok_per_s': 0.0}
        logger.info('LLM cloud OK | provider=%s | wall=%.1fms | reply_chars=%d', self.last_used_provider, wall_ms, len(reply_text))
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
        t0 = time.perf_counter()
        resp = requests.post('https://api.openai.com/v1/chat/completions', json=payload, headers=headers, proxies=self._get_proxies(cloud_cfg), timeout=35)
        resp.raise_for_status()
        data = resp.json()
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        self.last_used_provider = f'openai ({model})'
        self.last_timings = {'provider': self.last_used_provider, 'wall_ms': wall_ms, 'ttft_ms': wall_ms, 'eval_ms': 0.0, 'tok_per_s': 0.0}
        reply_len = len((data.get('choices', [{}])[0].get('message', {}).get('content') or ''))
        logger.info('LLM cloud OK | provider=%s | wall=%.1fms | reply_chars=%d', self.last_used_provider, wall_ms, reply_len)
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
                logger.warning('LLM FALLBACK triggered: %s -> local (reason=%s)', target, exc, exc_info=True)

        if not self._ensure_model_loaded():
            raise RuntimeError('LLM server is unavailable.')
        self.last_used_provider = 'local'
        return self._post(messages)

    def chat_stream(self, messages: List[Dict[str, str]], provider: Optional[str] = None) -> Generator[str, None, None]:
        resp = self.chat(messages, provider=provider)
        content = (resp.get('choices', [{}])[0].get('message', {}).get('content') or '')
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