"""Agent 性能评估器。

评估指标设计（结合竞品分析业务场景）：
- 信息覆盖度：采集到的竞品信息是否覆盖了 research_plan 中定义的所有维度
- 证据质量：高可信度证据占比（high credibility / total）
- 报告完整度：报告是否包含所有模板要求的章节
- 端到端延迟：从 start 到 report 完成的总耗时
- Token 效率：总 token 消耗 / 有效信息产出比
- 反思有效性：补采后信息覆盖度提升幅度
"""

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 报告模板中必须包含的章节关键词
REQUIRED_REPORT_SECTIONS = [
    "执行摘要",
    "竞品概览",
    "功能对比",
    "竞争格局",
    "建议",
]


@dataclass
class EvaluationResult:
    """单次分析的评估结果。"""

    # 信息覆盖度 (0-1)：research_plan 维度被采集数据覆盖的比例
    coverage_score: float = 0.0
    # 证据质量 (0-1)：高可信度证据占比
    evidence_quality: float = 0.0
    # 报告完整度 (0-1)：必须章节出现比例
    report_completeness: float = 0.0
    # 端到端延迟（秒）
    latency_seconds: float = 0.0
    # 总 token 消耗
    total_tokens: int = 0
    # Token 效率：有效信息量(证据数) / 千token
    token_efficiency: float = 0.0
    # 反思有效性：是否触发了补采且补采有效
    reflection_effective: bool = False
    # 综合得分 (0-100)
    overall_score: float = 0.0
    # 各维度详情
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_analysis(
    research_plan: dict[str, Any],
    research_data: dict[str, Any],
    report_md: str,
    thinking_steps: list[dict[str, Any]],
    latency_seconds: float,
    total_tokens: int,
    research_rounds: int = 0,
) -> EvaluationResult:
    """对一次完整分析执行自动评估。

    Args:
        research_plan: 研究计划（含 dimensions 列表）
        research_data: 采集到的竞品数据 {competitor_name: info_dict}
        report_md: 生成的 Markdown 报告
        thinking_steps: 各节点执行记录
        latency_seconds: 端到端耗时
        total_tokens: 总 token 消耗
        research_rounds: 研究轮数（>0 表示触发了反思补采）
    """
    coverage = _calc_coverage(research_plan, research_data)
    evidence_quality = _calc_evidence_quality(research_data)
    report_completeness = _calc_report_completeness(report_md)
    token_efficiency = _calc_token_efficiency(research_data, total_tokens)
    reflection_effective = _calc_reflection_effective(research_rounds, research_data)

    # 综合得分：加权平均（覆盖度30% + 证据质量25% + 报告完整度25% + token效率10% + 反思10%）
    overall = (
        coverage * 30
        + evidence_quality * 25
        + report_completeness * 25
        + min(token_efficiency / 2.0, 1.0) * 10  # 归一化到 0-1
        + (0.1 if reflection_effective else 0.0) * 100 * 0.1
    )
    overall = min(round(overall, 1), 100.0)

    result = EvaluationResult(
        coverage_score=round(coverage, 3),
        evidence_quality=round(evidence_quality, 3),
        report_completeness=round(report_completeness, 3),
        latency_seconds=round(latency_seconds, 1),
        total_tokens=total_tokens,
        token_efficiency=round(token_efficiency, 3),
        reflection_effective=reflection_effective,
        overall_score=overall,
        details={
            "dimensions_planned": len(research_plan.get("dimensions", [])),
            "competitors_analyzed": len(research_data),
            "report_length": len(report_md),
            "research_rounds": research_rounds,
        },
    )

    logger.info(
        "Evaluation complete: overall=%.1f coverage=%.2f evidence=%.2f report=%.2f tokens=%d latency=%.1fs",
        overall, coverage, evidence_quality, report_completeness, total_tokens, latency_seconds,
    )
    return result


def _calc_coverage(research_plan: dict, research_data: dict) -> float:
    """计算信息覆盖度：research_plan 中的维度被竞品数据覆盖的比例。"""
    dimensions = research_plan.get("dimensions", [])
    if not dimensions or not research_data:
        return 0.0

    # 提取维度名称/关键词
    dimension_names = []
    for dim in dimensions:
        if isinstance(dim, dict):
            name = dim.get("name", "") or dim.get("dimension", "")
            if name:
                dimension_names.append(name.lower())
        elif isinstance(dim, str):
            dimension_names.append(dim.lower())

    if not dimension_names:
        return 0.5  # 无法评估时给中间值

    # 检查每个维度是否在任一竞品数据中被覆盖
    covered = 0
    for dim_name in dimension_names:
        dim_keywords = dim_name.split()
        for _comp_name, comp_data in research_data.items():
            if not isinstance(comp_data, dict):
                continue
            # 检查竞品数据的各字段是否包含维度关键词
            data_text = _flatten_dict_to_text(comp_data)
            if any(kw in data_text for kw in dim_keywords if len(kw) > 1):
                covered += 1
                break

    return covered / len(dimension_names)


def _calc_evidence_quality(research_data: dict) -> float:
    """计算证据质量：高可信度证据占比。"""
    total_evidences = 0
    high_quality = 0

    for _name, comp_data in research_data.items():
        if not isinstance(comp_data, dict):
            continue
        evidences = comp_data.get("evidences", [])
        for ev in evidences:
            if isinstance(ev, dict):
                total_evidences += 1
                credibility = ev.get("credibility", "low")
                if credibility == "high":
                    high_quality += 1
                elif credibility == "medium":
                    high_quality += 0.5  # 中等可信度计半分

    if total_evidences == 0:
        return 0.0
    return high_quality / total_evidences


def _calc_report_completeness(report_md: str) -> float:
    """计算报告完整度：必须章节出现比例。"""
    if not report_md:
        return 0.0

    found = 0
    for section in REQUIRED_REPORT_SECTIONS:
        # 支持模糊匹配（章节标题可能略有变化）
        if section in report_md or section.lower() in report_md.lower():
            found += 1

    return found / len(REQUIRED_REPORT_SECTIONS)


def _calc_token_efficiency(research_data: dict, total_tokens: int) -> float:
    """计算 Token 效率：有效证据数 / 千token。"""
    if total_tokens == 0:
        return 0.0

    total_evidences = 0
    for _name, comp_data in research_data.items():
        if isinstance(comp_data, dict):
            total_evidences += len(comp_data.get("evidences", []))

    return total_evidences / (total_tokens / 1000.0)


def _calc_reflection_effective(research_rounds: int, research_data: dict) -> bool:
    """判断反思是否有效：触发了补采且最终有足够证据。"""
    if research_rounds <= 0:
        return False

    # 如果触发了补采，且最终每个竞品平均有 >= 2 条证据，认为有效
    total_evidences = 0
    comp_count = len(research_data)
    for _name, comp_data in research_data.items():
        if isinstance(comp_data, dict):
            total_evidences += len(comp_data.get("evidences", []))

    if comp_count == 0:
        return False
    avg_evidences = total_evidences / comp_count
    return avg_evidences >= 2.0


def _flatten_dict_to_text(data: dict) -> str:
    """将字典值展平为小写文本，用于关键词匹配。"""
    parts = []
    for key, value in data.items():
        if key in ("evidences", "sources"):
            continue
        if isinstance(value, str):
            parts.append(value.lower())
        elif isinstance(value, list):
            parts.extend(str(v).lower() for v in value if v)
    return " ".join(parts)
