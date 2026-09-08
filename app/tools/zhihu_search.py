"""知乎开放平台数据接口客户端。

接入知乎开放平台（developer.zhihu.com）提供的：
- 全网搜索 global_search：跨站搜索（含知乎站内问答/文章/外部站点）
- 知乎搜索 zhihu_search：仅知乎站内内容
- 直答 zhida：知乎直答 LLM，可直接给出综合回答（推荐用于开放性问题）

鉴权（官方文档「鉴权」章节）：
- Authorization: Bearer <access_secret>
- X-Request-Timestamp: 秒级 Unix 时间戳

统一返回与 Tavily web_search 相同结构的字典列表：
    {"title": ..., "url": ..., "content": ...}
以便上层 web_search 无缝切换，兼容现有证据/来源管道。
"""

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

_RETRY_DELAY = 2
_CONCURRENCY_LIMIT = 8
_HTTP_TIMEOUT = 10

# 查询级缓存：同一 query 在 TTL 内直接复用结果
_CACHE_TTL_SECONDS = 3600
_CACHE_MAX_ENTRIES = 200

_semaphore: asyncio.Semaphore | None = None
_http_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_EM_HIGHLIGHT = re.compile(r"<em>|</em>")
_WHITESPACE = re.compile(r"\s+")


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
    if not settings.zhihu_api_key:
        return None
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    return _http_client


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.zhihu_api_key}",
        "X-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json",
    }


def _clean(text: str) -> str:
    text = _EM_HIGHLIGHT.sub("", text or "")
    text = text.replace("\u200b", "")
    return _WHITESPACE.sub(" ", text).strip()


async def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    client = _get_client()
    if not client:
        return None
    async with _get_semaphore():
        for attempt in range(2):
            try:
                response = await client.get(url, params=params, headers=_headers())
                response.raise_for_status()
                data = response.json()
                if data.get("Code") != 0 and data.get("code") != 0:
                    logger.warning("Zhihu API error %s: %s (%s)", url, data.get("Message") or data.get("message"), params)
                    return None
                return data
            except Exception as exc:
                if attempt == 0:
                    logger.warning("Zhihu API request failed for %s (attempt 1/2), retrying in %ds: %s", url, _RETRY_DELAY, exc)
                    await asyncio.sleep(_RETRY_DELAY)
                else:
                    logger.error("Zhihu API request failed for %s after 2 attempts: %s", url, exc)
                    return None
    return None


def _safe_count(raw: Any) -> int:
    """知乎数字字段容错：可能是 "1.2k"/"12+"/"3.4万" 等非纯数字字符串。

    提取数字部分换算为整数，失败回退 0，避免单条坏数据拖垮整次分析。
    """
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip().lower().replace(",", "")
    if not text:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|万|w|m|亿)?", text)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2) or ""
    if unit == "k" or unit == "w":
        value *= 1000
    elif unit == "m":
        value *= 1000000
    elif unit == "万":
        value *= 10000
    elif unit == "亿":
        value *= 100000000
    return int(value)


def _normalize_item(item: dict[str, Any], authority_default: str = "1") -> dict[str, Any]:
    url = str(item.get("Url") or "").strip()
    title = str(item.get("Title") or "").strip()
    content = str(item.get("ContentText") or "").strip()
    return {
        "title": _clean(title),
        "url": url,
        "content": _clean(content),
        "content_type": str(item.get("ContentType") or item.get("content_type") or "").strip(),
        "author": str(item.get("AuthorName") or "").strip(),
        "authority_level": str(item.get("AuthorityLevel") or authority_default).strip(),
        "vote_up_count": _safe_count(item.get("VoteUpCount")),
        "comment_count": _safe_count(item.get("CommentCount")),
    }


async def zhihu_search(query: str, max_results: int = 5, search_db: str = "all") -> list[dict[str, Any]]:
    """知乎全网搜索（global_search）—— 推荐，结果覆盖知乎+全网。

    归一化为 Tavily 结构，使上游无需改动。
    """
    cache_key = f"global:{query}:{max_results}:{search_db}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:max_results]

    data = await _get_json(
        "https://developer.zhihu.com/api/v1/content/global_search",
        {"Query": query, "Count": min(max(1, max_results), 20), "SearchDB": search_db},
    )
    items = (data or {}).get("Data") or {}
    results = [_normalize_item(item) for item in items.get("Items", [])]
    _cache_set(cache_key, results)
    return results[:max_results]


async def zhihu_site_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """知乎站内搜索（zhihu_search）—— 仅限知乎内容，e.g. 问答/文章。"""
    cache_key = f"site:{query}:{max_results}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:max_results]

    data = await _get_json(
        "https://developer.zhihu.com/api/v1/content/zhihu_search",
        {"Query": query, "Count": min(max(1, max_results), 10)},
    )
    items = (data or {}).get("Data") or {}
    results = [_normalize_item(item) for item in items.get("Items", [])]
    _cache_set(cache_key, results)
    return results[:max_results]


async def zhida(query: str, model: str = "zhida-fast-1p5") -> str:
    """知乎直答（zhida）—— 直接返回综合回答文本。

    适合需要"直接答案"的场景（如功能/定价/优缺点的开放性问题），
    模型档位：zhida-fast-1p5（快速）、zhida-thinking-1p5（深度思考）、zhida-agent。
    """
    settings = get_settings()
    if not settings.zhihu_api_key:
        return ""
    client = _get_client()
    if not client:
        return ""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "stream": False,
    }
    async with _get_semaphore():
        try:
            response = await client.post(
                "https://developer.zhihu.com/v1/chat/completions",
                json=payload,
                headers=_headers(),
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("Zhida returned empty choices for model %s: %s", model, str(data)[:300])
                return ""
            message = choices[0].get("message", {})
            content = str(message.get("content") or "").strip()
            return content
        except Exception as exc:
            logger.warning("Zhida request failed for model %s: %s", model, exc)
            return ""
