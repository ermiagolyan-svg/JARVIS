# C.E.L.E.S.T.E. Live Web & Google Search Engine
# Supports Serper (Google Search API), Google Custom Search JSON API,
# and Zero-Key automatic fallback via Google News RSS & DuckDuckGo HTML.

import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests


class WebSearchEngine:
    def __init__(self):
        self._config = None
        self._load_config()

    def _load_config(self):
        try:
            from Core.Config.config_loader import get_config
            self._config = get_config()
            self._config.load()
        except Exception:
            self._config = None

    def _get_settings(self) -> Dict[str, Any]:
        if self._config is None:
            return {}
        self._config.load()
        return self._config.get_section('web_search') or {}

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        ws_cfg = self._get_settings()
        proxy = ws_cfg.get('proxy_url', '').strip()
        if not proxy and self._config is not None:
            cloud_cfg = self._config.get_section('cloud_ai') or {}
            proxy = cloud_cfg.get('proxy_url', '').strip()
        if proxy:
            return {'http': proxy, 'https': proxy}
        return None

    def search(self, query: str, max_results: Optional[int] = None) -> Dict[str, Any]:
        query = (query or '').strip()
        if not query:
            return {'success': False, 'query': '', 'results': [], 'error': 'Empty search query'}

        cfg = self._get_settings()
        limit = max_results or int(cfg.get('max_results', 4))
        proxies = self._get_proxies()

        serper_key = cfg.get('serper_api_key', '').strip()
        google_key = cfg.get('google_api_key', '').strip()
        google_cx = cfg.get('google_cx', '').strip()

        # 1. Try Serper (Google Search API) if configured
        if serper_key:
            try:
                res = self._search_serper(query, serper_key, limit, proxies)
                if res:
                    return {'success': True, 'provider': 'google_serper', 'query': query, 'results': res}
            except Exception:
                pass

        # 2. Try Google Custom Search API if configured
        if google_key and google_cx:
            try:
                res = self._search_google_cse(query, google_key, google_cx, limit, proxies)
                if res:
                    return {'success': True, 'provider': 'google_cse', 'query': query, 'results': res}
            except Exception:
                pass

        # 3. Zero-Key Fallback: DuckDuckGo Web + Google News RSS
        combined: List[Dict[str, str]] = []
        try:
            ddg_res = self._search_duckduckgo(query, limit, proxies)
            combined.extend(ddg_res)
        except Exception:
            pass

        if len(combined) < limit:
            try:
                gnews_res = self._search_google_news_rss(query, limit - len(combined), proxies)
                combined.extend(gnews_res)
            except Exception:
                pass

        return {
            'success': len(combined) > 0,
            'provider': 'google_rss_ddg_hybrid',
            'query': query,
            'results': combined[:limit],
        }

    def _search_serper(self, query: str, api_key: str, limit: int, proxies: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
        resp = requests.post(
            'https://google.serper.dev/search',
            headers={'X-API-KEY': api_key, 'Content-Type': 'application/json'},
            data=json.dumps({'q': query, 'num': limit}),
            proxies=proxies,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        if data.get('answerBox'):
            ab = data['answerBox']
            results.append({
                'title': ab.get('title', 'Google Answer Box'),
                'snippet': ab.get('answer') or ab.get('snippet', ''),
                'url': ab.get('link', ''),
            })
        for item in data.get('organic', [])[:limit]:
            results.append({
                'title': item.get('title', ''),
                'snippet': item.get('snippet', ''),
                'url': item.get('link', ''),
            })
        return results[:limit]

    def _search_google_cse(self, query: str, api_key: str, cx: str, limit: int, proxies: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
        params = {'key': api_key, 'cx': cx, 'q': query, 'num': min(limit, 10)}
        resp = requests.get('https://www.googleapis.com/customsearch/v1', params=params, proxies=proxies, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get('items', [])[:limit]:
            results.append({
                'title': item.get('title', ''),
                'snippet': item.get('snippet', ''),
                'url': item.get('link', ''),
            })
        return results

    def _search_duckduckgo(self, query: str, limit: int, proxies: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.post(
            'https://html.duckduckgo.com/html/',
            data={'q': query},
            headers=headers,
            proxies=proxies,
            timeout=10,
        )
        resp.raise_for_status()
        raw_html = resp.text
        snippets = re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', raw_html, re.DOTALL)
        titles = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', raw_html, re.DOTALL)

        results = []
        for idx in range(min(len(titles), len(snippets), limit)):
            link, raw_title = titles[idx]
            clean_title = html.unescape(re.sub(r'<[^>]+>', '', raw_title)).strip()
            clean_snippet = html.unescape(re.sub(r'<[^>]+>', '', snippets[idx])).strip()
            results.append({'title': clean_title, 'snippet': clean_snippet, 'url': link})
        return results

    def _search_google_news_rss(self, query: str, limit: int, proxies: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
        encoded = urllib.parse.quote(query)
        has_persian = bool(re.search(r'[\u0600-\u06FF]', query))
        lang_params = 'hl=fa&gl=IR&ceid=IR:fa' if has_persian else 'hl=en-US&gl=US&ceid=US:en'
        url = f'https://news.google.com/rss/search?q={encoded}&{lang_params}'
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        results = []
        for item in root.findall('.//item')[:limit]:
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            pub_date = (item.findtext('pubDate') or '').strip()
            desc = html.unescape(re.sub(r'<[^>]+>', '', item.findtext('description') or '')).strip()
            snippet = f'{desc} ({pub_date})' if pub_date else desc
            results.append({'title': title, 'snippet': snippet, 'url': link})
        return results

    def format_results_for_prompt(self, search_data: Dict[str, Any]) -> str:
        results = search_data.get('results', [])
        if not results:
            return 'هیچ نتیجه‌ای در جستجوی آنلاین یافت نشد.'
        lines = [f"نتایج جستجوی آنلاین وب برای «{search_data.get('query', '')}»:"]
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. عنوان: {item.get('title', '')}\n   خلاصه: {item.get('snippet', '')}")
        return '\n'.join(lines)


_web_engine: Optional[WebSearchEngine] = None


def get_web_search_engine() -> WebSearchEngine:
    global _web_engine
    if _web_engine is None:
        _web_engine = WebSearchEngine()
    return _web_engine


if __name__ == '__main__':
    print('=== C.E.L.E.S.T.E. WebSearchEngine Self-Test ===')
    engine = get_web_search_engine()
    out = engine.search('هوش مصنوعی', max_results=2)
    print(f"[TEST] Search executed (provider={out.get('provider')}, results={len(out.get('results', []))})")
    print('[OK] WebSearchEngine ready.')