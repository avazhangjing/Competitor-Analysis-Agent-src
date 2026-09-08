import json
import logging
from typing import Any

from langchain_core.messages import SystemMessage

from .llm import ainvoke_with_retry, get_llm, parse_json_object
from .prompts.analyze import ANALYSIS_PROMPT
from .state import AnalysisResult, CompetitorInfo, Depth

logger = logging.getLogger(__name__)


async def analyze_competitors(track: str, competitors: list[CompetitorInfo], depth: Depth) -> AnalysisResult:
    llm = get_llm()
    if llm:
        try:
            # 精简 payload：去掉原始 evidence 详情，只保留结构化字段
            compact_data = [_compact_info(info) for info in competitors]
            prompt = ANALYSIS_PROMPT.format(
                track=track,
                depth=depth,
                competitors_json=json.dumps(compact_data, ensure_ascii=False, indent=2),
            )
            response = await ainvoke_with_retry(llm, [SystemMessage(content=prompt)])
            data = parse_json_object(response.content)
            result = _from_llm_result(data, competitors)
            if result.positioning_rows and result.feature_matrix:
                return result
        except Exception:
            # LLM 超时/失败时降级为确定性分析，不让异常毁掉整次分析
            logger.exception("Analyze failed for track '%s', using deterministic fallback", track)
    return _fallback_analysis(track, competitors, depth)


def _compact_info(info: CompetitorInfo) -> dict[str, Any]:
    """只保留分析所需的结构化字段，去掉占空间大的 evidences 详情。"""
    return {
        "name": info.name,
        "features": info.features,
        "capabilities": info.capabilities,
        "pricing": info.pricing,
        "pricing_freetier": info.pricing_freetier,
        "pricing_paid_min": info.pricing_paid_min,
        "target_users": info.target_users,
        "positioning": info.positioning,
        "key_selling_points": info.key_selling_points,
        "differentiation": info.differentiation,
        "tech_stack": info.tech_stack,
        "api_openness": info.api_openness,
        "strengths": info.strengths,
        "weaknesses": info.weaknesses,
        "sentiment": info.sentiment,
        "evidence_notes": info.evidence_notes[:5],
    }


def _from_llm_result(data: dict[str, Any], competitors: list[CompetitorInfo]) -> AnalysisResult:
    result = _base_result(competitors)
    result.positioning_rows = _list_of_dicts(data.get("positioning_rows")) or result.positioning_rows
    result.feature_matrix = data.get("feature_matrix", {}) or _build_feature_matrix(competitors)
    result.pricing_rows = _list_of_dicts(data.get("pricing_rows")) or result.pricing_rows
    result.sentiment_rows = _list_of_dicts(data.get("sentiment_rows")) or result.sentiment_rows
    result.battlecard_rows = _list_of_dicts(data.get("battlecard_rows")) or result.battlecard_rows
    result.swot = data.get("swot", {}) or result.swot
    result.opportunity_rows = _list_of_dicts(data.get("opportunity_rows")) or result.opportunity_rows
    result.insights = _list_of_strings(data.get("insights")) or result.insights
    return result


_EVALUATION_DIMENSIONS = [
    {"name": "核心能力完整度", "weight": "高", "description": "功能与能力的覆盖广度"},
    {"name": "生态与集成", "weight": "中", "description": "API、连接器、插件、生态"},
    {"name": "企业级能力", "weight": "中", "description": "权限、安全、审计、治理"},
    {"name": "价格与商业门槛", "weight": "高", "description": "免费版限制、付费门槛"},
    {"name": "口碑与市场牵引", "weight": "中", "description": "用户评价与市场认知"},
    {"name": "易用性与上手成本", "weight": "中", "description": "上手速度、引导体验"},
]


def _base_result(competitors: list[CompetitorInfo]) -> AnalysisResult:
    return AnalysisResult(
        positioning_rows=[
            {
                "name": info.name,
                "positioning": info.positioning,
                "target_users": info.target_users,
                "selling_points": "、".join(info.key_selling_points),
                "differentiation": info.differentiation,
            }
            for info in competitors
        ],
        evaluation_dimensions=[dict(dim) for dim in _EVALUATION_DIMENSIONS],
        scorecard_rows=[_score_competitor(info, _EVALUATION_DIMENSIONS) for info in competitors],
        capability_rows=_build_capability_rows(competitors),
        pricing_rows=[
            {
                "name": info.name,
                "pricing": info.pricing,
                "free": info.pricing_freetier,
                "paid": info.pricing_paid_min,
                "enterprise": "以官网为准",
            }
            for info in competitors
        ],
        user_rows=[{"name": info.name, "target_users": info.target_users} for info in competitors],
        tech_rows=[{"name": info.name, "tech": info.tech_stack, "api": info.api_openness} for info in competitors],
        sentiment_rows=[{"name": info.name, "sentiment": info.sentiment} for info in competitors],
        swot={
            info.name: {
                "S": info.strengths or ["已具备公开可见的核心能力"],
                "W": info.weaknesses or ["部分细节公开信息有限"],
                "O": ["可结合目标细分用户做差异化包装"],
                "T": ["同类产品在生态、价格和品牌上持续竞争"],
            }
            for info in competitors
        },
    )


def _fallback_analysis(track: str, competitors: list[CompetitorInfo], depth: Depth) -> AnalysisResult:
    result = _base_result(competitors)
    result.feature_matrix = _build_feature_matrix(competitors)
    result.battlecard_rows = [_battlecard(info) for info in competitors]
    result.opportunity_rows = [
        {
            "area": "核心功能覆盖",
            "evidence": "从采集数据看，各竞品在核心功能上存在差异。",
            "recommendation": "优先补齐与头部竞品的功能差距，确保核心场景可用。",
        },
        {
            "area": "用户体验优化",
            "evidence": "用户口碑中 frequently 提到上手成本和易用性。",
            "recommendation": "优化首次使用引导，降低学习成本，提供模板和示例。",
        },
        {
            "area": "定价策略",
            "evidence": "免费版限制和付费门槛是用户决策的关键因素。",
            "recommendation": "设计有竞争力的免费版，突出付费版的独特价值。",
        },
    ]
    result.insights = [
        f"{track} 赛道竞争激烈，各竞品在功能覆盖、定价和用户体验上各有侧重。",
        "建议优先关注竞品的差异化定位，避免功能层面的直接对标。",
        "用户口碑和上手体验是重要的竞争维度，应持续优化。",
    ]
    return result


def _score_competitor(info: CompetitorInfo, dimensions: list[dict[str, str]]) -> dict[str, Any]:
    text = " ".join(
        [
            " ".join(info.features),
            " ".join(info.capabilities),
            info.pricing,
            info.target_users,
            info.positioning,
            " ".join(info.key_selling_points),
            info.differentiation,
            info.tech_stack,
            info.api_openness,
            " ".join(info.evidence_notes),
        ]
    )
    scores: dict[str, int] = {}
    for dimension in dimensions:
        name = dimension["name"]
        score = 3
        if name == "核心能力完整度":
            score = min(5, 2 + len(set(info.features + info.capabilities)) // 2)
        elif name == "生态与集成":
            score = _keyword_score(text, ["api", "API", "集成", "连接器", "插件", "生态", "marketplace"])
        elif name == "企业级能力":
            score = _keyword_score(text, ["权限", "安全", "审计", "治理", "企业", "SSO", "私有化", "团队"])
        elif name == "价格与商业门槛":
            score = 4 if any(word in info.pricing + info.pricing_freetier for word in ["免费", "free", "试用"]) else 3
        elif name == "口碑与市场牵引":
            score = 4 if info.sentiment or info.strengths else 3
        elif name == "易用性与上手成本":
            score = _keyword_score(text, ["模板", "拖拽", "可视化", "低代码", "无代码", "简单", "易用", "快速"])
        scores[name] = score
    total = round(sum(scores.values()) / max(len(scores), 1), 1)
    confidence = "中" if info.evidence_notes or info.sources else "低"
    return {
        "name": info.name,
        "scores": scores,
        "total": total,
        "confidence": confidence,
        "reason": _first_non_empty(info.differentiation, info.positioning, "基于已采集的功能、定价、定位和来源摘要综合评分"),
        "evidence_ids": _evidence_ids(info),
    }


def _build_capability_rows(competitors: list[CompetitorInfo]) -> list[dict[str, Any]]:
    capabilities = _top_capabilities(competitors)
    rows: list[dict[str, Any]] = []
    for capability in capabilities:
        values: dict[str, str] = {}
        for info in competitors:
            text = " ".join(info.features + info.capabilities + info.evidence_notes)
            if capability in text:
                rating = "强"
                note = "公开资料或采集信息明确提到该能力"
                confidence = "中"
            elif any(part and part in text for part in capability.split()):
                rating = "中"
                note = "有相邻能力线索，但表述不完全一致"
                confidence = "低"
            else:
                rating = "未披露"
                note = "当前采集证据未覆盖"
                confidence = "低"
            values[info.name] = f"{rating}｜{note}｜{confidence}"
        rows.append({"capability": capability, "importance": "高", "values": values, "evidence_ids": _matching_evidence_ids(competitors, capability)})
    return rows


def _top_capabilities(competitors: list[CompetitorInfo]) -> list[str]:
    counts: dict[str, int] = {}
    for info in competitors:
        for capability in info.capabilities or info.features:
            capability = capability.strip()
            if capability:
                counts[capability] = counts.get(capability, 0) + 1
    ranked = sorted(counts, key=lambda item: (-counts[item], item))
    return ranked[:8] or ["核心流程覆盖", "生态集成", "权限治理", "定价灵活性", "用户体验"]


def _battlecard(info: CompetitorInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "best_for": _first_non_empty(info.target_users, info.positioning, "目标用户需结合官网进一步确认"),
        "strengths": info.strengths or info.key_selling_points or ["公开资料显示具备基础竞争力"],
        "watchouts": info.weaknesses or ["公开信息有限，关键短板需通过试用或销售访谈验证"],
    }


def _evidence_ids(info: CompetitorInfo, limit: int = 3) -> list[str]:
    return [evidence.id for evidence in info.evidences[:limit]]


def _all_evidence_ids(competitors: list[CompetitorInfo], limit: int = 6) -> list[str]:
    ids: list[str] = []
    for info in competitors:
        ids.extend(_evidence_ids(info, limit=2))
    return ids[:limit]


def _matching_evidence_ids(competitors: list[CompetitorInfo], capability: str, limit: int = 4) -> list[str]:
    ids: list[str] = []
    for info in competitors:
        for evidence in info.evidences:
            if capability in evidence.snippet or capability in " ".join(info.capabilities + info.features):
                ids.append(evidence.id)
                break
    return ids[:limit]


def _keyword_score(text: str, keywords: list[str]) -> int:
    hits = sum(1 for keyword in keywords if keyword in text)
    return min(5, 2 + hits)


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _build_feature_matrix(competitors: list[CompetitorInfo]) -> dict[str, dict[str, str]]:
    all_features: dict[str, int] = {}
    for info in competitors:
        for feature in info.features:
            key = feature.strip()
            if key:
                all_features[key] = all_features.get(key, 0) + 1
    matrix: dict[str, dict[str, str]] = {}
    for feature, count in sorted(all_features.items(), key=lambda x: -x[1])[:20]:
        row: dict[str, str] = {}
        for info in competitors:
            feature_texts = [f.strip() for f in info.features]
            capability_texts = [c.strip() for c in info.capabilities]
            all_text = " ".join(feature_texts + capability_texts).lower()
            if feature.lower() in all_text:
                row[info.name] = "✓"
            elif any(part.lower() in all_text for part in feature.split() if len(part) > 1):
                row[info.name] = "△"
            else:
                row[info.name] = "✗"
        matrix[feature] = row
    return matrix
