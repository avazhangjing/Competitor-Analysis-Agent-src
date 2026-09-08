import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

from ..config import get_settings
from .bocha_search import bocha_search
from .zhihu_search import zhihu_search, zhihu_site_search, zhida

logger = logging.getLogger(__name__)

SearchProvider = str  # "auto" | "tavily" | "bocha" | "zhihu" | "zhihu_site" | "zhida"

_RETRY_DELAY = 2
_CONCURRENCY_LIMIT = 8

# 查询级缓存：同一 query 在 TTL 内直接复用结果，省 Tavily 额度与延迟
_CACHE_TTL_SECONDS = 3600
_CACHE_MAX_ENTRIES = 200

_semaphore: asyncio.Semaphore | None = None
_tavily_client: Any = None
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


def _get_client():
    """复用 Tavily 客户端，避免每次搜索都创建新连接。"""
    global _tavily_client
    settings = get_settings()
    if not settings.tavily_api_key:
        return None
    if _tavily_client is None:
        from tavily import AsyncTavilyClient
        _tavily_client = AsyncTavilyClient(api_key=settings.tavily_api_key)
    return _tavily_client


def resolve_provider(provider: SearchProvider | None) -> SearchProvider | None:
    """根据配置与请求解析最终搜索来源。

    - provider 显式传入（tavily/bocha/zhihu/zhihu_site/zhida）则直接采用；
    - provider 为 auto/None 时：有 bocha key 用 bocha，否则有 Tavily key 用 tavily，
      再否则用 zhihu（有 key 时），三者都无则返回 None（跳过搜索）。
    """
    settings = get_settings()
    requested = (provider or settings.search_provider or "auto").strip().lower()

    if requested == "tavily":
        return "tavily" if settings.tavily_api_key else None
    if requested == "bocha":
        return "bocha" if settings.bocha_api_key else None
    if requested in ("zhihu", "zhihu_site", "zhida"):
        return requested if settings.zhihu_api_key else None
    # auto / 其他
    if settings.bocha_api_key:
        return "bocha"
    if settings.tavily_api_key:
        return "tavily"
    return requested if requested in ("zhihu", "zhihu_site", "zhida") and settings.zhihu_api_key else None


async def _tavily_search(query: str, max_results: int) -> list[dict[str, Any]]:
    """Tavily 搜索（含缓存/并发限流/重试），供 tavily 主路径与博查失败回退复用。"""
    cached = _cache_get(query)
    if cached is not None:
        logger.info("Web search cache hit for '%s' (%d results)", query, len(cached))
        return cached[:max_results]

    client = _get_client()
    if not client:
        logger.warning("Search API key not configured, skipping search: %s", query)
        return []

    async with _get_semaphore():
        for attempt in range(2):
            try:
                response = await client.search(query=query, max_results=max_results, search_depth="basic")
                results = response.get("results", [])
                logger.info("Web search '%s' returned %d results", query, len(results))
                _cache_set(query, results)
                return results
            except Exception as exc:
                if attempt == 0:
                    logger.warning("Web search failed for '%s' (attempt 1/2), retrying in %ds: %s", query, _RETRY_DELAY, exc)
                    await asyncio.sleep(_RETRY_DELAY)
                else:
                    logger.error("Web search failed for '%s' after 2 attempts: %s", query, exc)
                    return []
    return []


async def web_search(query: str, max_results: int = 3, provider: SearchProvider | None = None) -> list[dict[str, Any]]:
    """按搜索来源执行搜索，返回统一结构 list[dict]。

    结构兼容：{"title", "url", "content", ...}，供证据/来源管道使用。
    auto 模式下博查限流/失败时自动回退 tavily → zhihu，避免搜索空结果；
    显式指定 bocha 时保持来源可控，不静默回退。
    """
    resolved = resolve_provider(provider)
    explicit = (provider or "").strip().lower() not in ("", "auto", "none")

    if resolved == "bocha":
        results = await bocha_search(query, max_results=max_results)
        if results or explicit:
            return results
        logger.warning("Bocha returned no results for '%s', falling back to next provider", query)
        if resolve_provider("tavily"):
            fb = await _tavily_search(query, max_results)
            if fb:
                logger.info("Fell back to tavily for '%s' (%d results)", query, len(fb))
                return fb
        if resolve_provider("zhihu"):
            fb = await zhihu_search(query, max_results=max_results)
            if fb:
                logger.info("Fell back to zhihu for '%s' (%d results)", query, len(fb))
                return fb
        return []

    if resolved in ("zhihu", "zhihu_site", "zhida"):
        if resolved == "zhihu_site":
            return await zhihu_site_search(query, max_results=max_results)
        if resolved == "zhida":
            # 知乎直答是综合回答接口，包装为统一结果结构（避免静默回退到 Tavily）
            try:
                answer = await zhida(query)
            except Exception:
                logger.exception("zhida failed for %s", query)
                answer = ""
            if not answer:
                return []
            # 每条直答结果必须携带独立的 url/title（知乎站内搜索页）：
            # 若所有 query 共用同一 (title, url)，证据与来源按 url 去重后会全部塌缩成一条，
            # 导致报告引用编号全部指向同一来源、信息来源章节其余竞品无可点击链接
            return [{
                "title": f"知乎直答：{query}"[:100],
                "url": f"https://www.zhihu.com/search?q={quote(query, safe='')}",
                "content": answer,
                "provider": "zhida",
            }]
        return await zhihu_search(query, max_results=max_results)

    if resolved == "tavily":
        return await _tavily_search(query, max_results)

    return []


async def web_answer(query: str, provider: SearchProvider | None = None) -> str:
    """直答：获取知乎直答综合回答。仅当 provider 为 zhida 或 auto 且未配置 tavily 时使用。"""
    if not resolve_provider(provider):
        return ""
    return await zhida(query)
