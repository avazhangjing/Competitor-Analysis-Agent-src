import logging
from typing import Any

from langchain_core.messages import SystemMessage

from .llm import ainvoke_with_retry, get_llm, parse_json_object
from .prompts.reflect import REFLECT_PROMPT
from .state import CompetitorInfo, Depth, MAX_RESEARCH_ROUNDS

logger = logging.getLogger(__name__)


async def reflect_on_research(
    track: str,
    depth: Depth,
    research_plan: dict[str, Any],
    competitors: list[CompetitorInfo],
    research_round: int,
) -> dict[str, Any]:
    llm = get_llm()
    if not llm:
        return {"ready_for_analysis": True, "gaps": [], "overall_assessment": "无 LLM，跳过反思直接进入分析"}

    if research_round >= MAX_RESEARCH_ROUNDS:
        return {"ready_for_analysis": True, "gaps": [], "overall_assessment": f"已达到最大检索轮数 {MAX_RESEARCH_ROUNDS}，进入分析"}

    research_dimensions = _format_dimensions(research_plan)
    research_summary = _format_research_summary(competitors)

    try:
        prompt = REFLECT_PROMPT.format(
            track=track,
            depth=depth,
            research_dimensions=research_dimensions,
            research_summary=research_summary,
        )
        response = await ainvoke_with_retry(llm, [SystemMessage(content=prompt)])
        data = parse_json_object(response.content)
    except Exception:
        logger.exception("Reflection failed for track '%s'", track)
        return {"ready_for_analysis": True, "gaps": [], "overall_assessment": "反思失败，进入分析"}

    ready = bool(data.get("ready_for_analysis", False))
    gaps = _normalize_gaps(data.get("gaps", []))

    if not gaps:
        ready = True

    return {
        "ready_for_analysis": ready,
        "gaps": gaps,
        "overall_assessment": str(data.get("overall_assessment", "")).strip(),
    }


def _format_dimensions(research_plan: dict[str, Any]) -> str:
    dimensions = research_plan.get("dimensions", [])
    if not dimensions:
        return "无研究计划维度"
    lines: list[str] = []
    for dim in dimensions:
        name = dim.get("name", "")
        questions = dim.get("key_questions", [])
        lines.append(f"- {name}：{'；'.join(questions) if questions else '无关键问题'}")
    return "\n".join(lines)


def _format_research_summary(competitors: list[CompetitorInfo]) -> str:
    lines: list[str] = []
    for info in competitors:
        lines.append(f"### {info.name}")
        lines.append(f"- 定位：{info.positioning or '未采集'}")
        lines.append(f"- 功能：{', '.join(info.features) if info.features else '未采集'}")
        lines.append(f"- 定价：{info.pricing or '未采集'}")
        lines.append(f"- 目标用户：{info.target_users or '未采集'}")
        lines.append(f"- 技术栈：{info.tech_stack or '未采集'}")
        lines.append(f"- 用户口碑：{info.sentiment or '未采集'}")
        evidence_count = len(info.evidences)
        high_credibility = sum(1 for e in info.evidences if e.credibility == "high")
        medium_credibility = sum(1 for e in info.evidences if e.credibility == "medium")
        lines.append(f"- 证据：共{evidence_count}条（高可信{high_credibility}，中可信{medium_credibility}）")
        lines.append("")
    return "\n".join(lines)


def _normalize_gaps(raw: list[Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        competitor = str(item.get("competitor", "")).strip()
        dimension = str(item.get("dimension", "")).strip()
        if not competitor or not dimension:
            continue
        queries = [str(q).strip() for q in item.get("followup_queries", []) if str(q).strip()]
        if not queries:
            queries = [f"{competitor} {dimension}"]
        gaps.append({
            "competitor": competitor,
            "dimension": dimension,
            "reason": str(item.get("reason", "")).strip(),
            "followup_queries": queries[:2],
        })
    return gaps[:5]
