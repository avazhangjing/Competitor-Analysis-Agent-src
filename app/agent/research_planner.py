import logging
from typing import Any

from langchain_core.messages import SystemMessage

from .llm import ainvoke_with_retry, get_llm, parse_json_object
from .prompts.research_plan import RESEARCH_PLAN_PROMPT
from .state import ResearchPlan

logger = logging.getLogger(__name__)


async def plan_research(track: str, memory_context: str = "", template: dict[str, Any] | None = None) -> dict[str, Any]:
    # 用户选择了默认/自定义模版：直接按模版维度与关键词构建研究计划，
    # 保证用户自定义的维度/关键词被精确使用（不走 LLM 重排，避免被改写）。
    if template and template.get("dimensions"):
        return _build_plan_from_template(track, template)

    llm = get_llm()
    if not llm:
        return _fallback_plan(track)

    # 将长期记忆上下文注入 prompt，实现经验复用
    prompt_content = RESEARCH_PLAN_PROMPT.format(track=track)
    if memory_context:
        prompt_content = prompt_content + "\n\n" + memory_context

    try:
        response = await ainvoke_with_retry(llm, [SystemMessage(content=prompt_content)])
        data = parse_json_object(response.content)
    except Exception:
        logger.exception("Research planning failed for track '%s'", track)
        return _fallback_plan(track)

    if not data or not data.get("dimensions"):
        return _fallback_plan(track)

    plan = ResearchPlan(
        track_intent=str(data.get("track_intent", "")).strip(),
        dimensions=_normalize_dimensions(data.get("dimensions", [])),
        hypotheses=[str(h).strip() for h in data.get("hypotheses", []) if str(h).strip()],
        report_outline=[str(s).strip() for s in data.get("report_outline", []) if str(s).strip()],
        search_strategy=str(data.get("search_strategy", "")).strip(),
    )
    return plan.to_dict()


def _build_plan_from_template(track: str, template: dict[str, Any]) -> dict[str, Any]:
    """按模版的维度与关键词直接生成研究计划（确定性，不依赖 LLM）。"""
    raw_dims = template.get("dimensions", [])
    dimensions: list[dict[str, Any]] = []
    for item in raw_dims:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        keywords = [str(k).strip() for k in item.get("keywords", []) if str(k).strip()]
        key_questions_raw = [str(k).strip() for k in item.get("key_questions", []) if str(k).strip()]
        # 搜索查询 = 赛道 + 维度关键词（中英双语尽量覆盖）
        searches: list[str] = [f"{track} {name}"]
        for kw in keywords:
            searches.append(f"{track} {kw}")
            searches.append(f"best {track} {kw}")
        # 去重并裁剪到每维度 5 条
        seen: set[str] = set()
        deduped: list[str] = []
        for q in searches:
            key = q.casefold()
            if q and key not in seen:
                seen.add(key)
                deduped.append(q)
            if len(deduped) >= 5:
                break
        if not key_questions_raw:
            key_questions_raw = [f"各竞品在{track}场景下「{name}」维度的表现与差异是什么？"]
        dimensions.append({
            "name": name,
            "key_questions": key_questions_raw[:3],
            "search_queries": deduped,
            # 用户自定义维度同等重要，统一按 high 处理，保证全部进入采集
            "priority": "high",
        })

    plan = ResearchPlan(
        track_intent=f"{track}赛道的直接竞品分析",
        dimensions=dimensions,
        hypotheses=[
            f"{track}赛道存在明确的头部产品和细分玩家分化",
            "定价策略是用户选择的关键决策因素",
            "功能覆盖度差异是主要竞争维度",
        ],
        report_outline=[
            "核心结论",
            "产品定位对比",
            template.get("name") + "维度对比" if template.get("name") else "功能对比矩阵",
            "定价策略对比",
            "用户体验对比",
            "优劣势总结",
            "对我们的启示",
            "信息来源",
        ],
        search_strategy="依据所选模版的维度与关键词进行多角度检索，中英双语覆盖",
    )
    return plan.to_dict()


def _normalize_dimensions(raw: list[Any]) -> list[dict[str, Any]]:
    dimensions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        dimensions.append({
            "name": name,
            "key_questions": [str(q).strip() for q in item.get("key_questions", []) if str(q).strip()],
            "search_queries": [str(q).strip() for q in item.get("search_queries", []) if str(q).strip()],
            "priority": str(item.get("priority", "medium")).strip(),
        })
    return dimensions


def _fallback_plan(track: str) -> dict[str, Any]:
    plan = ResearchPlan(
        track_intent=f"{track}赛道的直接竞品分析",
        dimensions=[
            {
                "name": "产品功能",
                "key_questions": [f"各竞品在{track}场景下支持哪些核心功能？"],
                "search_queries": [f"{track} 功能对比", f"best {track} features"],
                "priority": "high",
            },
            {
                "name": "定价策略",
                "key_questions": ["各竞品的定价模式和免费版限制是什么？"],
                "search_queries": [f"{track} pricing", f"{track} 定价"],
                "priority": "high",
            },
            {
                "name": "目标用户",
                "key_questions": ["各竞品面向哪些用户群体和行业？"],
                "search_queries": [f"{track} target users", f"{track} 用户群体"],
                "priority": "medium",
            },
            {
                "name": "产品定位",
                "key_questions": ["各竞品的差异化定位是什么？"],
                "search_queries": [f"{track} positioning", f"{track} 定位差异"],
                "priority": "medium",
            },
            {
                "name": "技术能力",
                "key_questions": ["各竞品的技术架构和API开放程度如何？"],
                "search_queries": [f"{track} API integration", f"{track} 技术架构"],
                "priority": "low",
            },
        ],
        hypotheses=[
            f"{track}赛道存在明确的头部产品和细分玩家分化",
            "定价策略是用户选择的关键决策因素",
            "功能覆盖度差异是主要竞争维度",
        ],
        report_outline=[
            "核心结论",
            "产品定位对比",
            "功能对比矩阵",
            "定价策略对比",
            "用户体验对比",
            "优劣势总结",
            "对我们的启示",
            "信息来源",
        ],
        search_strategy="覆盖产品功能、定价、用户、技术四个层面，中英双语检索",
    )
    return plan.to_dict()
