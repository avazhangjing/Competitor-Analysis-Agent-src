import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from langchain_core.messages import SystemMessage

from .llm import ainvoke_with_retry, get_llm, parse_json_object
from .prompts.search_discovery import DISCOVERY_PLAN_PROMPT
from .prompts.search_collect import DISCOVERY_PROMPT
from .prompts.search_collect_info import COLLECT_PROMPT
from .state import CompetitorInfo, Depth, Evidence, Source
from ..tools.web_fetch import web_fetch
from ..tools.web_search import web_search
from ..tools.mcp_client import query_company_info_via_mcp

logger = logging.getLogger(__name__)


async def discover_competitors(track: str, search_provider=None) -> list[dict[str, str]]:
    llm = get_llm()
    plan = await _build_discovery_plan(track, llm)
    search_summary = await _discovery_search_summary(plan["queries"], search_provider=search_provider)
    if llm:
        competitors = await _recommend_from_evidence(
            track=track,
            intent=plan["intent"],
            interpretations=plan["interpretations"],
            search_summary=search_summary,
            llm=llm,
        )
        if competitors:
            return _normalize_competitors(competitors)

    return _fallback_discovery(track, search_summary)


async def plan_competitor_discovery(track: str) -> dict[str, Any]:
    return await _build_discovery_plan(track, get_llm())


async def _build_discovery_plan(track: str, llm: Any | None) -> dict[str, Any]:
    fallback = {
        "intent": f"用户希望寻找“{track}”赛道下目标用户、核心任务和产品形态相近的直接竞品。",
        "needs_clarification": False,
        "interpretations": [],
        "queries": _default_discovery_queries(track),
    }
    if not llm:
        return fallback

    try:
        response = await ainvoke_with_retry(llm, [SystemMessage(content=DISCOVERY_PLAN_PROMPT.format(track=track))])
        data = parse_json_object(response.content)
    except Exception:
        return fallback

    intent = str(data.get("intent") or fallback["intent"]).strip()
    needs_clarification = bool(data.get("needs_clarification", False))
    interpretations = _normalize_interpretations(data.get("interpretations"))
    top_level_queries = [str(query).strip() for query in data.get("queries", []) if str(query).strip()]
    queries = _select_discovery_queries(interpretations, top_level_queries)
    if not queries:
        queries = fallback["queries"]
    return {
        "intent": intent,
        "needs_clarification": needs_clarification and len(interpretations) > 1,
        "interpretations": interpretations,
        "queries": queries,
    }


def _default_discovery_queries(track: str) -> list[str]:
    return [
        f"{track} 竞品 平台",
        f"{track} 替代品 对比",
        f"{track} competitors alternatives",
        f"best {track} platforms",
    ]


async def _discovery_search_summary(queries: list[str], search_provider=None) -> str:
    if not queries:
        return ""

    try:
        groups = await asyncio.gather(*(web_search(query, max_results=4, provider=search_provider) for query in queries))
    except Exception:
        return ""

    rows: list[str] = []
    seen_urls: set[str] = set()
    for group in groups:
        for result in group:
            url = str(result.get("url", "")).strip()
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            title = str(result.get("title", "")).strip()
            content = str(result.get("content", "")).strip()
            if title or content:
                rows.append(f"[{len(rows) + 1}] {title}: {content} ({url})")
            if len(rows) >= 12:
                return "\n".join(rows)
    return "\n".join(rows)


async def _recommend_from_evidence(
    track: str,
    intent: str,
    interpretations: list[dict[str, Any]],
    search_summary: str,
    llm: Any,
) -> list[Any]:
    try:
        response = await ainvoke_with_retry(
            llm,
            [
                SystemMessage(
                    content=DISCOVERY_PROMPT.format(
                        track=track,
                        intent=intent,
                        interpretations=_format_interpretations(interpretations),
                        search_summary=search_summary or "无搜索结果",
                    )
                )
            ]
        )
    except Exception:
        return []
    data = parse_json_object(response.content)
    competitors = data.get("competitors", [])
    return competitors if isinstance(competitors, list) else []


def _normalize_interpretations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    interpretations: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        description = str(item.get("description", "")).strip()
        queries = [str(query).strip() for query in item.get("queries", []) if str(query).strip()]
        if label or description or queries:
            interpretations.append({"label": label, "description": description, "queries": queries})
    return interpretations[:4]


def _select_discovery_queries(interpretations: list[dict[str, Any]], top_level_queries: list[str], limit: int = 8) -> list[str]:
    selected: list[str] = []
    max_queries_per_interpretation = 2 if len(interpretations) >= 3 else 3
    for item in interpretations:
        selected.extend(item.get("queries", [])[:max_queries_per_interpretation])
    selected.extend(top_level_queries)
    return _dedupe_strings(selected)[:limit]


def _format_interpretations(interpretations: list[dict[str, Any]]) -> str:
    if not interpretations:
        return "无"
    rows: list[str] = []
    for index, item in enumerate(interpretations, start=1):
        label = item.get("label") or f"解释{index}"
        description = item.get("description") or "无说明"
        rows.append(f"{index}. {label}：{description}")
    return "\n".join(rows)


def _normalize_competitors(raw_competitors: list[Any]) -> list[dict[str, str]]:
    competitors: list[dict[str, str]] = []
    for item in raw_competitors:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        brief = str(item.get("brief", "")).strip()
        if name:
            competitors.append({"name": name, "brief": brief or "系统推荐候选"})

    return _dedupe_competitors(competitors)[:8]


def _fallback_discovery(track: str, search_summary: str) -> list[dict[str, str]]:
    extracted = _extract_candidates_from_search_titles(search_summary)
    if extracted:
        return [{"name": name, "brief": "来自搜索结果的候选竞品，需人工确认相关性"} for name in extracted[:8]]
    return [
        {"name": f"{track}头部产品", "brief": "系统推荐候选"},
        {"name": f"{track}新兴产品", "brief": "系统推荐候选"},
        {"name": f"{track}国际产品", "brief": "系统推荐候选"},
    ]


def _dedupe_competitors(competitors: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for item in competitors:
        key = _competitor_key(item["name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _competitor_key(name: str) -> str:
    key = name.casefold()
    for char in [" ", "_", "-", "·", ".", "。", "（", "）", "(", ")"]:
        key = key.replace(char, "")
    return key


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _extract_candidates_from_search_titles(search_summary: str) -> list[str]:
    candidates: list[str] = []
    separators = [" vs ", " VS ", "对比", "替代", "竞品", "alternatives", "competitors", "：", ":"]
    for line in search_summary.splitlines():
        title = line.split(":", 1)[0]
        for separator in separators:
            title = title.replace(separator, ",")
        for token in title.split(","):
            token = token.strip(" []0123456789.-_")
            if 2 <= len(token) <= 40 and not any(word in token.lower() for word in ["best", "top", "平台", "工具", "list"]):
                candidates.append(token)
    return _dedupe_strings(candidates)


def _build_collect_queries(name: str, depth: Depth, research_plan: dict[str, Any] | None) -> list[str]:
    queries: list[str] = [
        f"{name} 功能 特性",
        f"{name} 定价 pricing",
        f"{name} 官网 产品定位",
    ]
    if depth == "deep":
        queries.extend([f"{name} API 集成", f"{name} 用户评价 优缺点"])

    if research_plan:
        dimensions = research_plan.get("dimensions", [])
        # 只取 high 优先级维度的第一条 query，避免过多重复搜索
        for dim in dimensions:
            if dim.get("priority") != "high":
                continue
            dim_queries = dim.get("search_queries", [])
            if dim_queries:
                personalized = f"{name} {dim_queries[0]}"
                if personalized not in queries and len(queries) < 6:
                    queries.append(personalized)
    return queries[:6]


async def collect_competitor(
    track: str,
    name: str,
    depth: Depth,
    extra_queries: list[str] | None = None,
    existing_info: CompetitorInfo | None = None,
    research_plan: dict[str, Any] | None = None,
    use_mcp: bool = True,
    search_provider=None,
) -> CompetitorInfo:
    queries = _build_collect_queries(name, depth, research_plan)
    if extra_queries:
        queries.extend(extra_queries[:3])

    search_results = await asyncio.gather(*(web_search(q, max_results=2, provider=search_provider) for q in queries))
    flat_results: list[dict[str, Any]] = [item for group in search_results for item in group]

    existing_urls: set[str] = set()
    if existing_info:
        for ev in existing_info.evidences:
            if ev.url:
                existing_urls.add(ev.url)

    new_results = [r for r in flat_results if str(r.get("url", "")).strip() not in existing_urls]
    all_results = new_results if new_results else flat_results

    evidences: list[Evidence] = _search_evidences(name, all_results[:8])
    search_summary = "\n".join(
        f"- [{evidence.id}] {evidence.title}: {evidence.snippet} ({evidence.url})"
        for evidence in evidences
    )
    fetched_pages = await _fetch_evidence_pages(all_results[:2])
    if fetched_pages:
        evidences.extend(_fetch_evidences(name, fetched_pages, start=len(evidences) + 1))
        fetched_snippets = [page["snippet"] for page in fetched_pages]
        search_summary = "\n".join([search_summary, "", "网页正文摘录：", *fetched_snippets]).strip()

    # 知乎直答补充：当搜索来源为知乎系时，调用直答获取综合回答，作为补充证据
    from ..tools.web_search import resolve_provider as _resolve_provider

    if _resolve_provider(search_provider) in ("zhihu", "zhihu_site", "zhida"):
        from ..tools.zhihu_search import zhida

        zhida_q = f"请综合介绍{name}：包括它的产品功能、定价策略、目标用户、产品定位、核心卖点、优缺点和用户口碑，控制在300字以内。"
        zhida_text = await zhida(zhida_q)
        if zhida_text:
            zhida_note = Evidence(
                id=f"{_evidence_prefix(name)}-zhida",
                competitor=name,
                source_type="web_search",
                title=f"{name} 知乎直答综合回答",
                url="zhihu_zhida",
                snippet=zhida_text[:900],
                credibility="medium",
                collected_at=datetime.now().isoformat(timespec="seconds"),
            )
            evidences.append(zhida_note)
            zhida_summary = f"\n\n知乎直答综合回答：{zhida_text[:800]}"
            search_summary += zhida_summary

    # MCP 工具调用：获取企业结构化信息（融资、团队规模等）
    mcp_info = {}
    if use_mcp:
        try:
            mcp_info = await query_company_info_via_mcp(name)
            if mcp_info and not mcp_info.get("error"):
                mcp_summary = f"\n\nMCP企业信息查询结果：{json.dumps(mcp_info, ensure_ascii=False)[:500]}"
                search_summary += mcp_summary
        except Exception as exc:
            logger.debug("MCP query failed for %s: %s", name, exc)

    data: dict[str, Any] = {}
    llm = get_llm()
    if llm:
        try:
            prompt = COLLECT_PROMPT.format(track=track, name=name, depth=depth, search_summary=search_summary or "无")
            response = await ainvoke_with_retry(llm, [SystemMessage(content=prompt)])
            data = parse_json_object(response.content)
        except Exception:
            # LLM 超时/失败时降级为通用兜底数据，不中断整次分析
            logger.warning("Collect LLM failed for %s, using fallback data", name)

    if not data:
        data = _fallback_collect(track, name, depth)
    if not evidences:
        evidences.append(_llm_fallback_evidence(name, data))

    if existing_info:
        data = _merge_collect_data(existing_info, data)
        evidences = _merge_evidences(existing_info.evidences, evidences)

    sources = [Source(label=f"{name} LLM知识库", url="llm_knowledge", type="llm_knowledge")]
    seen_urls: set[str] = set()
    for result in all_results[:5]:
        url = result.get("url")
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append(Source(label=result.get("title") or f"{name} 搜索结果", url=url, type="web_search"))
    for page in fetched_pages:
        if page["url"] not in seen_urls:
            seen_urls.add(page["url"])
            sources.append(Source(label=page["title"] or f"{name} 网页正文", url=page["url"], type="web_fetch"))
    if existing_info:
        for src in existing_info.sources:
            if (src.label, src.url) not in {(s.label, s.url) for s in sources}:
                sources.append(src)

    return CompetitorInfo(
        name=name,
        features=_list(data.get("features")),
        capabilities=_list(data.get("capabilities")) or _list(data.get("features")),
        pricing=str(data.get("pricing", "")),
        pricing_freetier=str(data.get("pricing_freetier", "")),
        pricing_paid_min=str(data.get("pricing_paid_min", "")),
        target_users=str(data.get("target_users", "")),
        positioning=str(data.get("positioning", "")),
        key_selling_points=_list(data.get("key_selling_points")),
        differentiation=str(data.get("differentiation", "")),
        tech_stack=str(data.get("tech_stack", "")),
        api_openness=str(data.get("api_openness", "")),
        strengths=_list(data.get("strengths")),
        weaknesses=_list(data.get("weaknesses")),
        sentiment=str(data.get("sentiment", "")),
        evidence_notes=_list(data.get("evidence_notes")),
        evidences=evidences,
        sources=sources,
    )


def _search_evidences(name: str, results: list[dict[str, Any]]) -> list[Evidence]:
    collected_at = datetime.now().isoformat(timespec="seconds")
    evidences: list[Evidence] = []
    seen_urls: set[str] = set()
    for result in results:
        url = str(result.get("url", "")).strip()
        title = str(result.get("title", "")).strip() or f"{name} 搜索结果"
        snippet = str(result.get("content", "")).strip()
        if not url and not snippet:
            continue
        key = url or f"{title}:{snippet[:80]}"
        if key in seen_urls:
            continue
        seen_urls.add(key)
        evidences.append(
            Evidence(
                id=f"{_evidence_prefix(name)}-{len(evidences) + 1}",
                competitor=name,
                source_type="web_search",
                title=title,
                url=url,
                snippet=snippet[:900],
                credibility=_credibility_for("web_search", url),
                collected_at=collected_at,
            )
        )
    return evidences


def _fetch_evidences(name: str, pages: list[dict[str, str]], start: int) -> list[Evidence]:
    collected_at = datetime.now().isoformat(timespec="seconds")
    evidences: list[Evidence] = []
    for index, page in enumerate(pages, start=start):
        evidences.append(
            Evidence(
                id=f"{_evidence_prefix(name)}-{index}",
                competitor=name,
                source_type="web_fetch",
                title=page["title"] or f"{name} 网页正文",
                url=page["url"],
                snippet=page["text"][:900],
                credibility=_credibility_for("web_fetch", page["url"]),
                collected_at=collected_at,
            )
        )
    return evidences


def _evidence_prefix(name: str) -> str:
    clean = "".join(ch for ch in name if ch.isalnum())[:16]
    return clean or "evidence"


def _credibility_for(source_type: str, url: str) -> str:
    if source_type == "web_fetch":
        return "high"
    if source_type == "web_search":
        return "medium"
    return "low"


def _llm_fallback_evidence(name: str, data: dict[str, Any]) -> Evidence:
    notes = _list(data.get("evidence_notes"))
    snippet = "；".join(notes) or str(data.get("positioning") or data.get("differentiation") or "LLM 常识兜底信息，需人工核验")
    return Evidence(
        id=f"{_evidence_prefix(name)}-1",
        competitor=name,
        source_type="llm_knowledge",
        title=f"{name} LLM 常识兜底",
        url="llm_knowledge",
        snippet=snippet[:900],
        credibility="low",
        collected_at=datetime.now().isoformat(timespec="seconds"),
    )


async def _fetch_evidence_pages(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    async def fetch_one(result: dict[str, Any]) -> dict[str, str] | None:
        url = str(result.get("url", "")).strip()
        title = str(result.get("title", "")).strip()
        if not url:
            return None
        try:
            text = await web_fetch(url, limit=1600)
        except Exception:
            return None
        if not text:
            return None
        return {"title": title, "url": url, "text": text[:1600], "snippet": f"- {title or url}: {text[:1600]} ({url})"}

    pages = await asyncio.gather(*(fetch_one(result) for result in results))
    return [page for page in pages if page]


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    return []


def _merge_collect_data(existing: CompetitorInfo, new_data: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "features": _dedupe_list(existing.features + _list(new_data.get("features"))),
        "capabilities": _dedupe_list(existing.capabilities + _list(new_data.get("capabilities"))) or _dedupe_list(existing.features + _list(new_data.get("features"))),
        "pricing": new_data.get("pricing", "") or existing.pricing,
        "pricing_freetier": new_data.get("pricing_freetier", "") or existing.pricing_freetier,
        "pricing_paid_min": new_data.get("pricing_paid_min", "") or existing.pricing_paid_min,
        "target_users": new_data.get("target_users", "") or existing.target_users,
        "positioning": new_data.get("positioning", "") or existing.positioning,
        "key_selling_points": _dedupe_list(existing.key_selling_points + _list(new_data.get("key_selling_points"))),
        "differentiation": new_data.get("differentiation", "") or existing.differentiation,
        "tech_stack": new_data.get("tech_stack", "") or existing.tech_stack,
        "api_openness": new_data.get("api_openness", "") or existing.api_openness,
        "strengths": _dedupe_list(existing.strengths + _list(new_data.get("strengths"))),
        "weaknesses": _dedupe_list(existing.weaknesses + _list(new_data.get("weaknesses"))),
        "sentiment": new_data.get("sentiment", "") or existing.sentiment,
        "evidence_notes": _dedupe_list(existing.evidence_notes + _list(new_data.get("evidence_notes"))),
    }
    return merged


def _merge_evidences(existing: list[Evidence], new_evidences: list[Evidence]) -> list[Evidence]:
    seen_urls: set[str] = set()
    for ev in existing:
        if ev.url:
            seen_urls.add(ev.url)
    result = list(existing)
    for ev in new_evidences:
        if ev.url and ev.url in seen_urls:
            continue
        if ev.url:
            seen_urls.add(ev.url)
        result.append(ev)
    return result


def _dedupe_list(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(item.strip())
    return result


def _fallback_collect(track: str, name: str, depth: Depth) -> dict[str, Any]:
    return {
        "features": [f"{track}核心功能", f"{track}基础能力"],
        "capabilities": [f"{track}核心流程覆盖", "基础配置能力"],
        "pricing": "具体定价需参考官网最新信息",
        "pricing_freetier": "免费版限制以官网为准",
        "pricing_paid_min": "以官网最新定价为准",
        "target_users": f"关注{track}效率的团队和组织",
        "positioning": f"{name} 是面向{track}场景的产品",
        "key_selling_points": [f"聚焦{track}核心场景"],
        "differentiation": f"差异主要体现在{track}领域的产品能力和生态",
        "tech_stack": "公开信息有限，需结合官网与开发者文档复核" if depth == "deep" else "",
        "api_openness": "具体开放程度以官网为准" if depth == "deep" else "",
        "strengths": [f"聚焦{track}核心场景"],
        "weaknesses": ["公开信息有限，需通过试用进一步验证"],
        "sentiment": f"用户通常关注{track}产品的易用性、稳定性和价格",
        "evidence_notes": ["未配置外部服务或结构化采集失败，当前为通用兜底信息"],
    }


# =============================================================================
# Function Calling 增强版采集（面试展示用）
# =============================================================================

async def collect_with_function_calling(
    track: str,
    name: str,
    depth: Depth,
) -> list[dict[str, Any]]:
    """使用 Function Calling 机制让 LLM 自主决定工具调用。

    面试话术：
    "这个函数展示了 Function Calling 的完整流程：
    1. 通过 bind_tools 将工具绑定到 LLM
    2. LLM 根据任务自主决定调用哪个工具、传什么参数
    3. 执行工具调用并将结果返回给 LLM
    4. 工具调用失败时有重试和降级机制"

    Args:
        track: 赛道
        name: 竞品名称
        depth: 分析深度

    Returns:
        采集到的证据列表
    """
    from ..tools import AGENT_TOOLS
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

    llm = get_llm()
    if not llm:
        return []

    # 1. 绑定工具到 LLM
    llm_with_tools = llm.bind_tools(AGENT_TOOLS)

    # 2. 构建初始消息
    system_prompt = (
        f"You are a competitive analysis research assistant. "
        f"Your task is to gather information about '{name}' in the '{track}' market. "
        f"Use the available tools to search for and fetch relevant information. "
        f"Focus on: features, pricing, target users, positioning, and strengths/weaknesses. "
        f"Make 2-4 tool calls to gather comprehensive information."
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"Research '{name}' - a competitor in the {track} space. Find key information about this product."),
    ]

    evidences: list[dict[str, Any]] = []
    max_tool_rounds = 3

    for _round in range(max_tool_rounds):
        try:
            response = await llm_with_tools.ainvoke(messages)
        except Exception as exc:
            logger.warning("Function calling LLM invoke failed: %s", exc)
            break

        messages.append(response)

        # 3. 检查是否有工具调用
        if not hasattr(response, "tool_calls") or not response.tool_calls:
            break

        # 4. 执行工具调用
        for tool_call in response.tool_calls:
            tool_name = tool_call.get("name", "")
            tool_args = tool_call.get("args", {})
            tool_id = tool_call.get("id", "")

            try:
                if tool_name == "web_search_tool":
                    result = await web_search(
                        query=tool_args.get("query", name),
                        max_results=tool_args.get("max_results", 3),
                    )
                    # 将搜索结果转化为证据
                    for r in result[:3]:
                        evidences.append({
                            "source_type": "web_search",
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": r.get("content", "")[:200],
                        })
                    tool_result = str(result)[:2000]

                elif tool_name == "web_fetch_tool":
                    result = await web_fetch(
                        url=tool_args.get("url", ""),
                        limit=tool_args.get("limit", 3000),
                    )
                    if result:
                        evidences.append({
                            "source_type": "web_fetch",
                            "title": tool_args.get("url", ""),
                            "url": tool_args.get("url", ""),
                            "snippet": result[:300],
                        })
                    tool_result = result[:2000] if result else "Failed to fetch content"
                else:
                    tool_result = f"Unknown tool: {tool_name}"

            except Exception as exc:
                # 5. 工具调用失败处理（重试/降级）
                logger.warning("Tool call %s failed: %s", tool_name, exc)
                tool_result = f"Tool call failed: {exc}. Please try a different approach."

            # 将工具结果返回给 LLM
            messages.append(ToolMessage(content=tool_result, tool_call_id=tool_id))

    logger.info("Function calling collected %d evidences for %s", len(evidences), name)
    return evidences

