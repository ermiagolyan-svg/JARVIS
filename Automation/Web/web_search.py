# C.E.L.E.S.T.E. Live Web, Currency & Google Search Engine
import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests


class WebSearchEngine:
    FINGLISH_MAP = {
        'qeymat': 'قیمت',
        'gheimat': 'قیمت',
        'qeimat': 'قیمت',
        'dolar': 'دلار USD',
        'dollar': 'دلار USD',
        'lir': 'لیر ترکیه TRY',
        'turkey': 'ترکیه TRY',
        'toman': 'تومان',
        'tala': 'طلا',
        'sekke': 'سکه',
        'chande': 'چند است',
        'chand': 'چند',
        'emrooz': 'امروز',
        'akhbar': 'اخبار',
    }

    CURRENCY_TRIGGERS = [
        'dolar', 'dollar', 'usd', 'eur', 'euro', 'lir', 'try', 'gbp', 'aed', 'cad', 'jpy', 'cny',
        'دلار', 'لیر', 'یورو', 'پوند', 'درهم', 'ارز', 'نرخ', 'qeymat', 'gheimat', 'قیمت',
    ]

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

    def _normalize_query(self, query: str) -> str:
        # If query is written in Finglish (ASCII letters matching Finglish words), enrich it for search
        words = re.findall(r'[a-zA-Z0-9]+', query.lower())
        if words and any(w in self.FINGLISH_MAP for w in words):
            translated = [self.FINGLISH_MAP.get(w, w) for w in words]
            return ' '.join(translated)
        return query

    def _is_currency_query(self, query: str) -> bool:
        q_low = query.lower()
        return any(k in q_low for k in self.CURRENCY_TRIGGERS)

    def _fetch_live_fx_rates(self, proxies: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        try:
            resp = requests.get('https://open.er-api.com/v6/latest/USD', proxies=proxies, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            rates = data.get('rates', {})
            if not rates:
                return None
            try_rate = rates.get('TRY', 'N/A')
            eur_rate = rates.get('EUR', 'N/A')
            gbp_rate = rates.get('GBP', 'N/A')
            aed_rate = rates.get('AED', 'N/A')
            cad_rate = rates.get('CAD', 'N/A')
            irr_rate = rates.get('IRR', 'N/A')
            eur_to_try = round(float(try_rate) / float(eur_rate), 4) if isinstance(try_rate, (int, float)) and isinstance(eur_rate, (int, float)) and eur_rate else 'N/A'
            snippet = (
                f'نرخ لحظه‌ای بازار جهانی (پایه ۱ دلار آمریکا USD): '
                f'1 USD = {try_rate} TRY (لیر ترکیه) | '
                f'1 USD = {eur_rate} EUR (یورو) | '
                f'1 EUR = {eur_to_try} TRY | '
                f'1 USD = {AED_rate if "AED_rate" in locals() else aed_rate} AED (درهم) | '
                f'1 USD = {gbp_rate} GBP (پوند) | '
                f'1 USD = {cad_rate} CAD | '
                f'بروزرسانی زنده: {data.get("time_last_update_utc", "Today")}'
            )
            return {
                'title': 'Live Global Foreign Exchange Rates (1 USD Base)',
                'snippet': snippet,
                'url': 'https://open.er-api.com/v6/latest/USD',
            }
        except Exception:
            return None

    def search(self, query: str, max_results: Optional[int] = None) -> Dict[str, Any]:
        query = (query or '').strip()
        if not query:
            return {'success': False, 'query': '', 'results': [], 'error': 'Empty search query'}

        cfg = self._get_settings()
        limit = max_results or int(cfg.get('max_results', 4))
        proxies = self._get_proxies()
        normalized_q = self._normalize_query(query)

        combined: List[Dict[str, str]] = []

        # 0. If user asks about currency / exchange rates (USD, TRY, EUR, etc.), fetch live FX table first
        if self._is_currency_query(query):
            fx_item = self._fetch_live_fx_rates(proxies)
            if fx_item:
                combined.append(fx_item)

        serper_key = cfg.get('serper_api_key', '').strip()
        google_key = cfg.get('google_api_key', '').strip()
        google_cx = cfg.get('google_cx', '').strip()

        # 1. Serper API if configured
        if serper_key:
            try:
                res = self._search_serper(normalized_q, serper_key, limit, proxies)
                combined.extend(res)
            except Exception:
                pass

        # 2. Google CSE if configured
        if len(combined) < limit and google_key and google_cx:
            try:
                res = self._search_google_cse(normalized_q, google_key, google_cx, limit, proxies)
                combined.extend(res)
            except Exception:
                pass

        # 3. Zero-key DuckDuckGo HTML
        if len(combined) < limit:
            try:
                ddg_res = self._search_duckduckgo(normalized_q, limit, proxies)
                combined.extend(ddg_res)
            except Exception:
                pass

        # 4. Zero-key Google News RSS
        if len(combined) < limit:
            try:
                gnews_res = self._search_google_news_rss(normalized_q, limit - len(combined), proxies)
                combined.extend(gnews_res)
            except Exception:
                pass

        return {
            'success': len(combined) > 0,
            'provider': 'hybrid_live_web',
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
                'title': ab.get('title', 'Google Answer'),
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
        return [
            {'title': item.get('title', ''), 'snippet': item.get('snippet', ''), 'url': item.get('link', '')}
            for item in data.get('items', [])[:limit]
        ]

    def _search_duckduckgo(self, query: str, limit: int, proxies: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.post(
            'https://html.duckduckgo.com/html/',
            data={'q': query},
            headers=headers,
            proxies=proxies,
            timeout=8,
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
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=8)
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
        lines = [f"اطلاعات زنده دریافت‌شده از اینترنت برای «{search_data.get('query', '')}»:"]
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. {item.get('title', '')}: {item.get('snippet', '')}")
        return '\n'.join(lines)


_web_engine: Optional[WebSearchEngine] = None


def get_web_search_engine() -> WebSearchEngine:
    global _web_engine
    if _web_engine is None:
        _web_engine = WebSearchEngine()
    return _web_engine


if __name__ == '__main__':
    engine = get_web_search_engine()
    res = engine.search('qeymat 1 usd be lir turkey chande')
    print(engine.format_results_for_prompt(res))