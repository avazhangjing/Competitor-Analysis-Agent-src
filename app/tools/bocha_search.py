"""博查（Bocha）Web Search API 客户端。

官方文档：https://bocha-ai.feishu.cn/wiki/RXEOw02rFiwzGSkd9mUcqoeAnNK
- 端点：POST https://api.bochaai.com/v1/web-search
- 鉴权：Authorization: Bearer <API_KEY>
- 请求体：{"query", "freshness"(可选), "summary"(可选), "count"(默认 10, 最大 50)}
- 响应：{"code": 200, "data": {"webPages": {"value": [{"name", "url", "snippet", "summary", "siteName", ...}]}}}

统一返回与 Tavily web_search 相同结构的字典列表：
    {"title": ..., "url": ..., "content": ...}
以便上层 web_search 无缝切换，兼容现有证据/来源管道。
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

_RETRY_DELAY = 2
_CONCURRENCY_LIMIT = 4
_HTTP_TIMEOUT = 15

# 429 限流重试间隔（秒）：指数退避 + Retry-After 头优先
_RATE_LIMIT_DELAYS = (2, 5)

# 查询级缓存：同一 query 在 TTL 内直接复用结果
_CACHE_TTL_SECONDS = 3600
_CACHE_MAX_ENTRIES = 200

_BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"

_semaphore: asyncio.Semaphore | None = None
_http_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _cache_get(query: str) -> list[dict[str, Any]] | None:
    hit = _cache.get(query)
    if hit is None:
        return None
    if time.monotonic() - hit[0] > _CACHE_TTL_SECONDS:
        _cache.pop(query, None)
        return None
    return [dict(r) for r in hit[1]]


def _cache_set(query: str, results: list[dict[str, Any]]) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[query] = (time.monotonic(), results)


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_CONCURRENCY_LIMIT)
    return _semaphore


def _get_client() -> httpx.AsyncClient | None:
    global _http_client
    settings = get_settings()
    if not settings.bocha_api_key:
        return None
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    return _http_client


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.bocha_api_key}",
        "Content-Type": "application/json",
    }


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    url = str(item.get("url") or "").strip()
    title = str(item.get("name") or "").strip()
    content = str(item.get("summary") or item.get("snippet") or "").strip()
    return {
        "title": title,
        "url": url,
        "content": content,
        "provider": "bocha",
        "site_name": str(item.get("siteName") or "").strip(),
        "display_url": str(item.get("displayUrl") or "").strip(),
        "date_published": str(item.get("datePublished") or "").strip(),
    }


def _retry_after_seconds(headers: Any, default: int) -> int:
    """解析 Retry-After 头（秒或 HTTP 日期），失败回退默认值。"""
    if not headers:
        return default
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return default


async def bocha_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """博查 Web Search API 全网搜索。

    归一化为 Tavily 结构，使上游无需改动。开启 summary 让结果附带综合摘要。
    429 限流：按 Retry-After/指数退避最多重试 3 次，仍失败返回 []（由上层决定是否回退）。
    """
    max_results = min(max(1, max_results), 50)
    cache_key = f"bocha:{query}:{max_results}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:max_results]

    client = _get_client()
    if not client:
        logger.warning("Bocha API key not configured, skipping search: %s", query)
        return []

    payload = {"query": query, "summary": True, "count": max_results}

    async with _get_semaphore():
        for attempt in range(3):
            try:
                response = await client.post(_BOCHA_API_URL, json=payload, headers=_headers())
                response.raise_for_status()
                data = response.json()
                if data.get("code") != 200:
                    logger.warning("Bocha API error %s: %s", data.get("code"), data.get("msg"))
                    return []
                web_pages = (data.get("data") or {}).get("webPages") or {}
                results = [_normalize_item(item) for item in web_pages.get("value", []) if item.get("url")]
                logger.info("Bocha search '%s' returned %d results", query, len(results))
                _cache_set(cache_key, results)
                return results[:max_results]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    wait = _retry_after_seconds(
                        exc.response.headers,
                        _RATE_LIMIT_DELAYS[attempt] if attempt < len(_RATE_LIMIT_DELAYS) else 10,
                    )
                    logger.warning("Bocha rate limited (429) for '%s', retrying in %ds (attempt %d/3)", query, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if attempt < 2:
                    logger.warning("Bocha search failed for '%s' (attempt %d/3), retrying in %ds: %s", query, attempt + 1, _RETRY_DELAY, exc)
                    await asyncio.sleep(_RETRY_DELAY)
                else:
                    logger.error("Bocha search failed for '%s' after 3 attempts: %s", query, exc)
                    return []
            except Exception as exc:
                if attempt < 2:
                    logger.warning("Bocha search failed for '%s' (attempt %d/3), retrying in %ds: %s", query, attempt + 1, _RETRY_DELAY, exc)
                    await asyncio.sleep(_RETRY_DELAY)
                else:
                    logger.error("Bocha search failed for '%s' after 3 attempts: %s", query, exc)
                    return []
    logger.error("Bocha search for '%s' rate limited after 3 attempts", query)
    return []
