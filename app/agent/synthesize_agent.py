import json
import logging
from typing import Any

from langchain_core.messages import SystemMessage

from .llm import ainvoke_with_retry, get_llm, parse_json_object
from .prompts.synthesize import SYNTHESIZE_PROMPT
from .analyze_agent import _fallback_analysis as build_provisional_analysis
from .state import AnalysisResult, CompetitorInfo, Depth, Synthesis

logger = logging.getLogger(__name__)


async def synthesize_analysis(
    track: str,
    depth: Depth,
    research_plan: dict[str, Any],
    competitors: list[CompetitorInfo],
) -> dict[str, Any]:
    """综合分析（与结构化分析并行执行）。

    输入使用确定性的初步分析数据（基于采集结果直接构建），
    因此本阶段不依赖 LLM 分析结果，可与之并发运行，显著缩短整体耗时。
    """
    llm = get_llm()
    provisional = build_provisional_analysis(track, competitors, depth)
    if not llm:
        return _fallback_synthesis(track, competitors, provisional).to_dict()

    hypotheses = research_plan.get("hypotheses", [])
    analysis_json = json.dumps({
        "positioning_rows": provisional.positioning_rows,
        "feature_matrix": provisional.feature_matrix,
        "scorecard_rows": provisional.scorecard_rows,
        "capability_rows": provisional.capability_rows,
        "battlecard_rows": provisional.battlecard_rows,
        "pricing_rows": provisional.pricing_rows,
        "sentiment_rows": provisional.sentiment_rows,
        "swot": provisional.swot,
        "opportunity_rows": provisional.opportunity_rows,
        "insights": provisional.insights,
    }, ensure_ascii=False, indent=2)

    evidence_summary = _build_evidence_summary(competitors)

    try:
        prompt = SYNTHESIZE_PROMPT.format(
            track=track,
            depth=depth,
            hypotheses=_format_hypotheses(hypotheses),
            analysis_json=analysis_json,
            evidence_summary=evidence_summary,
        )
        response = await ainvoke_with_retry(llm, [SystemMessage(content=prompt)])
        data = parse_json_object(response.content)
    except Exception:
        logger.exception("Synthesis failed for track '%s'", track)
        return _fallback_synthesis(track, competitors, provisional).to_dict()

    if not data or not data.get("executive_summary"):
        return _fallback_synthesis(track, competitors, provisional).to_dict()

    synthesis = Synthesis(
        executive_summary=str(data.get("executive_summary", "")).strip(),
        competitive_landscape=str(data.get("competitive_landscape", "")).strip(),
        key_insights=[str(i).strip() for i in data.get("key_insights", []) if str(i).strip()],
        strategic_recommendations=[str(r).strip() for r in data.get("strategic_recommendations", []) if str(r).strip()],
        risk_assessment=str(data.get("risk_assessment", "")).strip(),
        narrative_sections=[
            {"section": str(s.get("section", "")).strip(), "content": str(s.get("content", "")).strip()}
            for s in data.get("narrative_sections", [])
            if isinstance(s, dict) and s.get("section")
        ],
    )
    return synthesis.to_dict()


def _format_hypotheses(hypotheses: list[str]) -> str:
    if not hypotheses:
        return "无初始假设"
    return "\n".join(f"- {h}" for h in hypotheses)


def _build_evidence_summary(competitors: list[CompetitorInfo]) -> str:
    lines: list[str] = []
    for info in competitors:
        lines.append(f"### {info.name}")
        lines.append(f"- 定位：{info.positioning}")
        lines.append(f"- 核心功能：{', '.join(info.features[:5])}")
        lines.append(f"- 定价：{info.pricing}（免费版：{info.pricing_freetier}，起步价：{info.pricing_paid_min}）")
        lines.append(f"- 目标用户：{info.target_users}")
        lines.append(f"- 优势：{', '.join(info.strengths[:3])}")
        lines.append(f"- 劣势：{', '.join(info.weaknesses[:3])}")
        if info.sentiment:
            lines.append(f"- 口碑：{info.sentiment}")
        high_ev = [e for e in info.evidences if e.credibility == "high"]
        med_ev = [e for e in info.evidences if e.credibility == "medium"]
        lines.append(f"- 证据：高可信{len(high_ev)}条（网页抓取），中可信{len(med_ev)}条（搜索结果）")
        lines.append("")
    return "\n".join(lines)


def _fallback_synthesis(track: str, competitors: list[CompetitorInfo], analysis: AnalysisResult) -> Synthesis:
    leader_name = ""
    if analysis.scorecard_rows:
        sorted_rows = sorted(analysis.scorecard_rows, key=lambda r: _safe_float(r.get("total")), reverse=True)
        if sorted_rows:
            leader_name = sorted_rows[0].get("name", "")

    summary = f"{track} 赛道共有 {len(competitors)} 个主要竞品。"
    if leader_name:
        summary += f"综合评分最高的是 {leader_name}。"
    if analysis.insights:
        summary += " ".join(analysis.insights[:2])

    landscape = f"{track} 赛道竞争格局呈现多梯队分布。"
    if analysis.positioning_rows:
        landscape += "各竞品在定位上各有侧重：" + "；".join(
            f"{r.get('name', '')}主打{r.get('differentiation', r.get('selling_points', ''))}"
            for r in analysis.positioning_rows[:3]
        ) + "。"

    insights = analysis.insights if analysis.insights else [
        f"{track}赛道竞争激烈，各竞品在功能覆盖和定价策略上各有侧重。",
        "建议关注竞品的差异化定位，避免功能层面的直接对标。",
    ]

    recommendations = []
    if analysis.opportunity_rows:
        recommendations = [opp.get("recommendation", "") for opp in analysis.opportunity_rows if opp.get("recommendation")]
    if not recommendations:
        recommendations = [
            "优先补齐核心功能覆盖，确保核心场景可用",
            "设计有竞争力的免费版，突出付费版的独特价值",
            "关注竞品未覆盖的细分场景，建立差异化优势",
        ]

    risk = f"需持续关注{track}赛道的竞品动态变化、定价策略调整和技术趋势演进。"

    return Synthesis(
        executive_summary=summary,
        competitive_landscape=landscape,
        key_insights=insights,
        strategic_recommendations=recommendations,
        risk_assessment=risk,
        narrative_sections=[],
    )


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
