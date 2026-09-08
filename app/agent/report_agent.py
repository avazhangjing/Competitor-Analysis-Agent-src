from datetime import datetime
import re
from typing import Any

from .state import AnalysisResult, CompetitorInfo, Depth, Source, Synthesis


def build_report(
    track: str,
    depth: Depth,
    competitors: list[CompetitorInfo],
    analysis: AnalysisResult,
    synthesis: dict[str, Any] | None = None,
    research_plan: dict[str, Any] | None = None,
) -> str:
    depth_label = "广度优先" if depth == "broad" else "深度优先"
    syn = _build_synthesis(synthesis)
    lines: list[str] = [
        f"# {track} 竞品分析报告",
        "",
        f"> 分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} | 分析深度：{depth_label} | 竞品数量：{len(competitors)}",
        "",
    ]

    if research_plan and research_plan.get("track_intent"):
        lines.append(f"> 赛道边界：{research_plan['track_intent']}")
        lines.append("")

    lines.extend(["---", "", "## 一、核心结论", ""])
    lines.extend(_executive_summary_section(track, competitors, analysis, syn))

    if syn and syn.competitive_landscape:
        lines.extend(["", "### 竞争格局", "", syn.competitive_landscape])

    lines.extend(["", "## 二、产品定位对比", ""])
    narrative = _get_narrative(syn, "产品定位分析")
    if narrative:
        lines.extend([narrative, ""])
    lines.extend(_positioning_section(analysis))

    lines.extend(["", "## 三、功能对比矩阵", ""])
    narrative = _get_narrative(syn, "功能竞争态势")
    if narrative:
        lines.extend([narrative, ""])
    lines.extend(_feature_matrix_section(analysis, competitors))

    lines.extend(["", "## 四、定价策略对比", ""])
    narrative = _get_narrative(syn, "定价策略洞察")
    if narrative:
        lines.extend([narrative, ""])
    lines.extend(_pricing_section(analysis))

    lines.extend(["", "## 五、用户体验对比", ""])
    narrative = _get_narrative(syn, "用户群体画像")
    if narrative:
        lines.extend([narrative, ""])
    lines.extend(_ux_section(analysis, competitors))

    lines.extend(["", "## 六、优劣势总结", ""])
    lines.extend(_battlecard_section(analysis))

    if depth == "deep":
        lines.extend(["", "## 七、SWOT 分析", ""])
        lines.extend(_swot_section(analysis))

    chapter_offset = 8 if depth == "deep" else 7
    lines.extend(["", f"## {_cn_number(chapter_offset)}、关键洞察", ""])
    lines.extend(_insights_section(syn, analysis))

    chapter_offset += 1
    lines.extend(["", f"## {_cn_number(chapter_offset)}、战略建议", ""])
    lines.extend(_recommendations_section(syn, analysis, track))

    chapter_offset += 1
    if syn and syn.risk_assessment:
        lines.extend(["", f"## {_cn_number(chapter_offset)}、风险评估", ""])
        lines.extend([syn.risk_assessment])
        chapter_offset += 1

    lines.extend(["", f"## {_cn_number(chapter_offset)}、对我们的启示", ""])
    lines.extend(_action_items_section(track, analysis, syn))

    chapter_offset += 1
    lines.extend(["", f"## {_cn_number(chapter_offset)}、信息来源", ""])
    sources = collect_sources(competitors)
    lines.extend(_sources_section(competitors, sources))

    report_md = "\n".join(lines).strip() + "\n"
    return _inject_citations(report_md, competitors, sources)


def _executive_summary_section(
    track: str,
    competitors: list[CompetitorInfo],
    analysis: AnalysisResult,
    syn: Synthesis | None = None,
) -> list[str]:
    lines: list[str] = []

    if syn and syn.executive_summary:
        lines.append(f"**执行摘要**：{syn.executive_summary}")
        lines.append("")
        return lines

    leader = _get_leader(analysis)
    if leader:
        lines.append(f"**赛道格局**：{track} 赛道中，**{leader.get('name', '')}** 综合表现领先，主要优势在于 {_clean(leader.get('reason', '产品能力和市场覆盖'))}。")
    else:
        lines.append(f"**赛道格局**：{track} 赛道竞争较为均衡，各竞品在不同维度各有优势。")

    lines.append("")

    if analysis.insights:
        lines.append("**关键洞察**：")
        for insight in analysis.insights[:3]:
            lines.append(f"- {insight}")
        lines.append("")

    lines.append("**行动建议**：")
    if analysis.opportunity_rows:
        for opp in analysis.opportunity_rows[:2]:
            lines.append(f"- {opp.get('recommendation', '')}")
    else:
        lines.append("- 优先补齐核心功能覆盖，再考虑差异化")
        lines.append("- 关注竞品的定价策略和免费版限制")

    return lines


def _positioning_section(analysis: AnalysisResult) -> list[str]:
    if not analysis.positioning_rows:
        return ["暂无定位数据"]

    lines = [
        "| 竞品 | 一句话定位 | 目标用户 | 核心卖点 | 差异化 |",
        "|------|-----------|---------|---------|--------|",
    ]
    for row in analysis.positioning_rows:
        lines.append(
            f"| {_clean(row.get('name', ''))} | {_clean(row.get('positioning', ''))} | "
            f"{_clean(row.get('target_users', ''))} | {_clean(row.get('selling_points', ''))} | "
            f"{_clean(row.get('differentiation', ''))} |"
        )
    return lines


def _feature_matrix_section(analysis: AnalysisResult, competitors: list[CompetitorInfo]) -> list[str]:
    if not analysis.feature_matrix:
        return ["暂无功能对比数据"]

    lines = [
        "| 功能模块 | 功能点 | " + " | ".join(_clean(info.name) for info in competitors) + " |",
        "|---------|--------|" + "|".join(["------"] * len(competitors)) + "|",
    ]

    modules = _group_features_by_module(analysis.feature_matrix)
    for module, features in modules.items():
        for feature_name, support_map in features.items():
            cells = []
            for info in competitors:
                support = support_map.get(info.name, "✗")
                cells.append(_format_support(support))
            lines.append(f"| {_clean(module)} | {_clean(feature_name)} | " + " | ".join(cells) + " |")

    return lines


def _group_features_by_module(feature_matrix: dict[str, dict[str, str]]) -> dict[str, dict[str, dict[str, str]]]:
    modules: dict[str, dict[str, dict[str, str]]] = {}
    for feature, support_map in feature_matrix.items():
        module = _infer_module(feature)
        if module not in modules:
            modules[module] = {}
        modules[module][feature] = support_map
    return modules


def _infer_module(feature: str) -> str:
    feature_lower = feature.lower()
    if any(kw in feature_lower for kw in ["文档", "编辑", "markdown", "笔记"]):
        return "文档"
    if any(kw in feature_lower for kw in ["表格", "数据库", "database"]):
        return "数据管理"
    if any(kw in feature_lower for kw in ["协作", "协同", "评论", "共享"]):
        return "协作"
    if any(kw in feature_lower for kw in ["任务", "项目", "看板", "kanban"]):
        return "项目管理"
    if any(kw in feature_lower for kw in ["api", "集成", "插件", "扩展"]):
        return "集成与扩展"
    if any(kw in feature_lower for kw in ["权限", "安全", "审计", "企业"]):
        return "企业级能力"
    return "核心功能"


def _format_support(support: str) -> str:
    support_lower = support.lower()
    if support in ["✓", "yes", "true", "支持", "有"]:
        return "✓"
    if support in ["✗", "no", "false", "不支持", "无"]:
        return "✗"
    if support in ["△", "partial", "部分", "部分支持"]:
        return "△"
    return support


def _pricing_section(analysis: AnalysisResult) -> list[str]:
    if not analysis.pricing_rows:
        return ["暂无定价数据"]

    lines = [
        "| 竞品 | 定价模式 | 免费版 | 起步价 | 企业版 |",
        "|------|---------|--------|--------|--------|",
    ]
    for row in analysis.pricing_rows:
        lines.append(
            f"| {_clean(row.get('name', ''))} | {_clean(row.get('pricing', ''))} | "
            f"{_clean(row.get('free', ''))} | {_clean(row.get('paid', ''))} | "
            f"{_clean(row.get('enterprise', '以官网为准'))} |"
        )

    lines.append("")
    lines.append("> **定价策略分析**：关注竞品的免费版限制和付费门槛，这直接影响用户转化和市场竞争策略。")

    return lines


def _ux_section(analysis: AnalysisResult, competitors: list[CompetitorInfo]) -> list[str]:
    lines: list[str] = []

    if analysis.sentiment_rows:
        lines.extend([
            "**用户口碑**：",
            "",
            "| 竞品 | 好评关键词 | 差评关键词 |",
            "|------|-----------|-----------|",
        ])
        for row in analysis.sentiment_rows:
            sentiment = _clean(row.get("sentiment", ""))
            positive, negative = _extract_sentiment_keywords(sentiment)
            lines.append(f"| {_clean(row.get('name', ''))} | {positive} | {negative} |")
        lines.append("")

    lines.extend([
        "**上手体验评估**：",
        "",
        "| 竞品 | 注册流程 | 首次使用引导 | 核心功能可达性 |",
        "|------|---------|-------------|--------------|",
    ])
    for info in competitors:
        reg_steps = "3步以内（支持第三方登录）" if info.positioning and "简单" in info.positioning else "标准注册流程"
        onboarding = "有模板/引导" if info.features and any("模板" in f for f in info.features) else "基础引导"
        accessibility = "较易（主打易用）" if info.key_selling_points and any("易用" in p for p in info.key_selling_points) else "中等学习曲线"
        lines.append(f"| {_clean(info.name)} | {reg_steps} | {onboarding} | {accessibility} |")

    return lines


def _extract_sentiment_keywords(sentiment: str) -> tuple[str, str]:
    if not sentiment:
        return "暂无数据", "暂无数据"
    positive_keywords = []
    negative_keywords = []
    if "易用" in sentiment or "简单" in sentiment:
        positive_keywords.append("易用性")
    if "稳定" in sentiment:
        positive_keywords.append("稳定性")
    if "价格" in sentiment or "便宜" in sentiment:
        positive_keywords.append("性价比")
    if "复杂" in sentiment or "难用" in sentiment:
        negative_keywords.append("学习成本高")
    if "贵" in sentiment:
        negative_keywords.append("价格较高")
    if "bug" in sentiment.lower() or "问题" in sentiment:
        negative_keywords.append("稳定性问题")

    positive = "、".join(positive_keywords) if positive_keywords else "口碑整体中性"
    negative = "、".join(negative_keywords) if negative_keywords else "未见明显槽点"
    return positive, negative


def _battlecard_section(analysis: AnalysisResult) -> list[str]:
    if not analysis.battlecard_rows:
        return ["暂无优劣势数据"]

    lines = [
        "| 竞品 | 核心优势 | 主要短板 | 适合谁 |",
        "|------|---------|---------|--------|",
    ]
    for row in analysis.battlecard_rows:
        strengths = row.get("strengths", [])
        if isinstance(strengths, list):
            strengths_text = "、".join(strengths[:2]) if strengths else "暂无"
        else:
            strengths_text = str(strengths)[:30]

        watchouts = row.get("watchouts", [])
        if isinstance(watchouts, list):
            watchouts_text = "、".join(watchouts[:2]) if watchouts else "暂无"
        else:
            watchouts_text = str(watchouts)[:30]

        lines.append(
            f"| {_clean(row.get('name', ''))} | {_clean(strengths_text)} | "
            f"{_clean(watchouts_text)} | {_clean(row.get('best_for', ''))} |"
        )

    return lines


def _swot_section(analysis: AnalysisResult) -> list[str]:
    if not analysis.swot:
        return ["暂无 SWOT 数据"]

    lines: list[str] = []
    for name, swot in analysis.swot.items():
        lines.extend([
            f"### {name}",
            "",
            "| 优势 (S) | 劣势 (W) |",
            "|---------|---------|",
            f"| {'；'.join(swot.get('S', ['暂无']))} | {'；'.join(swot.get('W', ['暂无']))} |",
            "",
            "| 机会 (O) | 威胁 (T) |",
            "|---------|---------|",
            f"| {'；'.join(swot.get('O', ['暂无']))} | {'；'.join(swot.get('T', ['暂无']))} |",
            "",
        ])

    return lines


def _action_items_section(track: str, analysis: AnalysisResult, syn: Synthesis | None = None) -> list[str]:
    lines: list[str] = []

    lines.extend([
        "### 应该做的",
        "",
    ])
    if syn and syn.strategic_recommendations:
        for rec in syn.strategic_recommendations[:4]:
            lines.append(f"- {rec}")
    elif analysis.opportunity_rows:
        for opp in analysis.opportunity_rows[:3]:
            lines.append(f"- {opp.get('recommendation', '')}")
    else:
        lines.append("- 优先补齐核心功能覆盖")
        lines.append("- 优化用户体验和上手流程")

    lines.extend(["", "### 不应该做的", ""])
    lines.append("- 避免盲目跟随竞品的所有功能，应聚焦自身优势场景")
    lines.append("- 不要在定价上直接对标头部竞品，应寻找差异化定位")

    lines.extend(["", "### 差异化机会", ""])
    if syn and syn.key_insights:
        for insight in syn.key_insights[:2]:
            lines.append(f"- {insight}")
    elif analysis.insights:
        for insight in analysis.insights[:2]:
            lines.append(f"- {insight}")
    else:
        lines.append("- 关注竞品未覆盖的细分场景")
        lines.append("- 在特定用户群体中建立口碑优势")

    return lines


def _insights_section(syn: Synthesis | None, analysis: AnalysisResult) -> list[str]:
    if syn and syn.key_insights:
        return [f"- {insight}" for insight in syn.key_insights]
    if analysis.insights:
        return [f"- {insight}" for insight in analysis.insights]
    return ["- 暂无关键洞察"]


def _recommendations_section(syn: Synthesis | None, analysis: AnalysisResult, track: str) -> list[str]:
    if syn and syn.strategic_recommendations:
        return [f"- {rec}" for rec in syn.strategic_recommendations]
    if analysis.opportunity_rows:
        return [f"- {opp.get('recommendation', '')}" for opp in analysis.opportunity_rows if opp.get("recommendation")]
    return [
        f"- 优先补齐{track}核心功能覆盖",
        "- 优化用户体验和上手流程",
        "- 设计有竞争力的免费版策略",
    ]


def _build_synthesis(synthesis_data: dict[str, Any] | None) -> Synthesis | None:
    if not synthesis_data:
        return None
    return Synthesis(
        executive_summary=str(synthesis_data.get("executive_summary", "")),
        competitive_landscape=str(synthesis_data.get("competitive_landscape", "")),
        key_insights=[str(i) for i in synthesis_data.get("key_insights", [])],
        strategic_recommendations=[str(r) for r in synthesis_data.get("strategic_recommendations", [])],
        risk_assessment=str(synthesis_data.get("risk_assessment", "")),
        narrative_sections=[
            {"section": str(s.get("section", "")), "content": str(s.get("content", ""))}
            for s in synthesis_data.get("narrative_sections", [])
            if isinstance(s, dict)
        ],
    )


def _get_narrative(syn: Synthesis | None, section_name: str) -> str:
    if not syn or not syn.narrative_sections:
        return ""
    for section in syn.narrative_sections:
        if section.get("section") == section_name:
            return section.get("content", "")
    return ""


def _cn_number(n: int) -> str:
    cn = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
          "十一", "十二", "十三", "十四", "十五"]
    if 0 < n < len(cn):
        return cn[n]
    return str(n)


_SOURCE_TYPE_LABELS = {"web_fetch": "官网", "web_search": "网页搜索", "llm_knowledge": "知识库"}


def _sources_section(competitors: list[CompetitorInfo], sources: list[Source]) -> list[str]:
    lines: list[str] = []

    # 全局编号（与正文 [[n]] 引用编号一致，不允许重排）
    idx_of = {(s.label, s.url): i + 1 for i, s in enumerate(sources)}

    # 按竞品分组；重复来源归属首个竞品，与 collect_sources 去重逻辑一致
    grouped: list[tuple[str, list[tuple[int, Source]]]] = []
    claimed: set[tuple[str, str]] = set()
    for info in competitors:
        items: list[tuple[int, Source]] = []
        for s in info.sources:
            key = (s.label, s.url)
            if key in idx_of and key not in claimed:
                items.append((idx_of[key], s))
                claimed.add(key)
        if items:
            grouped.append((_clean(info.name), sorted(items, key=lambda x: x[0])))

    web_total = sum(1 for s in sources if _is_web_url(s.url))
    lines.append(
        f"共 {len(sources)} 个来源，其中网页来源 {web_total} 篇、知识库补充 {len(sources) - web_total} 条"
    )
    lines.append("")

    for name, items in grouped:
        lines.append(f"### {name}（{len(items)} 个来源）")
        for i, s in items:
            badge = _SOURCE_TYPE_LABELS.get(s.type, s.type)
            if _is_web_url(s.url):
                lines.append(f"{i}. `{badge}` [{_clean(s.label)}]({s.url})")
            else:
                lines.append(f"{i}. `{badge}` {_clean(s.label)}")
        lines.append("")

    lines.append("> **说明**：正文中的 [[n]] 引用编号可点击跳转到对应网页；本报告基于 LLM 知识库、Web 搜索和网页抓取获取信息，建议对关键结论进行人工核验。")

    return lines


def _is_web_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _type_rank(source_type: str) -> int:
    """来源优先级：官网抓取 > 网页搜索 > 其他。"""
    return {"web_fetch": 0, "web_search": 1}.get(source_type, 2)


MAX_CITES_PER_LINE = 3
MAX_CITES_PER_CLAUSE = 1
# 从句边界：句号/问号/感叹号/逗号/分号（不含顿号，避免把并列的竞品名拆开）
_CLAUSE_RE = re.compile(r"[^。！？!?，;；\n]+[。！？!?，;；]|[^。！？!?，;；\n]+$")


def _citations_for_line(line: str, names: list[str], comp_indices: dict[str, list[int]]) -> list[tuple[int, list[int]]]:
    """按从句为单位分配引用：从句中提到竞品时，在该从句句末追加引用编号。

    相比按整句注入，从句级注入能把引用贴到具体论断（如
    “Notion 领先[[2]]，Obsidian 紧随其后[[3]]”），大幅减少张冠李戴。

    返回 [(插入位置, 编号列表), ...]，位置基于原行。编号按从句中竞品出现顺序
    逐家轮询取优先来源（官网 > 搜索）。
    """
    pairs: list[tuple[int, list[int]]] = []
    used: list[int] = []
    for m in _CLAUSE_RE.finditer(line):
        clause = m.group(0)
        comps = [
            p
            for _, p in sorted(
                (
                    (clause.index(name), comp_indices[name])
                    for name in names
                    if name and name in clause and comp_indices[name]
                ),
                key=lambda x: x[0],
            )
        ]
        idxs: list[int] = []
        level = 0
        while (
            len(idxs) < MAX_CITES_PER_CLAUSE
            and any(len(p) > level for p in comps)
        ):
            for p in comps:
                if level < len(p) and p[level] not in used and p[level] not in idxs:
                    idxs.append(p[level])
                if len(idxs) >= MAX_CITES_PER_CLAUSE:
                    break
            level += 1
        if idxs:
            used.extend(idxs)
            # 小标贴在从句标点之前（如 “领先[[1]]，Obsidian…”）
            pos = m.end()
            if m.group(0)[-1:] in "。！？!?，;；":
                pos -= 1
            pairs.append((pos, idxs))
        if len(used) >= MAX_CITES_PER_LINE:
            break
    return pairs


def _apply_citations(line: str, pairs: list[tuple[int, list[int]]]) -> str:
    if not pairs:
        return line
    parts: list[str] = []
    last = 0
    for pos, idxs in pairs:
        parts.append(line[last:pos])
        parts.append(f"[[{','.join(map(str, idxs))}]]")
        last = pos
    parts.append(line[last:])
    return "".join(parts)


def _inject_citations(markdown: str, competitors: list[CompetitorInfo], sources: list[Source]) -> str:
    """在报告正文中按从句注入 [[n]] 引用标记（仿 DeepSeek 对话风格）。

    - 仅正文文字注入，表格行、标题、引用块一律不加小标，避免密集噪音；
    - 从句中提到竞品时，在该从句句末追加该竞品的来源编号（每从句最多 1 个，每行最多 3 个），
      把引用贴到具体论断附近，减少跨竞品错配；
    - 优先官网抓取（web_fetch）来源；LLM 知识库（无 URL）不参与引用。
    编号与“信息来源”章节的全局编号保持一致。
    """
    idx_of = {(s.label, s.url): i + 1 for i, s in enumerate(sources)}
    comp_indices: dict[str, list[int]] = {}
    for info in competitors:
        comp_indices[info.name] = sorted(
            (
                idx_of[(s.label, s.url)]
                for s in info.sources
                if (s.label, s.url) in idx_of and _is_web_url(s.url)
            ),
            key=lambda i: (_type_rank(sources[i - 1].type), i),
        )
    names = sorted(comp_indices, key=len, reverse=True)

    lines = markdown.splitlines()
    out: list[str] = []
    in_sources = False
    for line in lines:
        stripped = line.strip()
        if in_sources:
            out.append(line)
            continue
        if stripped.startswith("## ") and "信息来源" in stripped:
            in_sources = True
            out.append(line)
            continue
        if "[[" in line:
            out.append(line)
            continue
        if (
            stripped.startswith("|")
            or stripped.startswith("#")
            or stripped.startswith(">")
            or stripped == "---"
        ):
            out.append(line)
            continue
        out.append(_apply_citations(line, _citations_for_line(line, names, comp_indices)))
    return "\n".join(out)


def _get_leader(analysis: AnalysisResult) -> dict[str, Any] | None:
    if not analysis.scorecard_rows:
        return None
    sorted_rows = sorted(
        [row for row in analysis.scorecard_rows if isinstance(row, dict)],
        key=lambda row: _score_value(row.get("total")),
        reverse=True,
    )
    return sorted_rows[0] if sorted_rows else None


def _score_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("|", "｜").split())


def collect_sources(competitors: list[CompetitorInfo]) -> list[Source]:
    seen: set[tuple[str, str]] = set()
    sources: list[Source] = []
    for info in competitors:
        for source in info.sources:
            key = (source.label, source.url)
            if key not in seen:
                seen.add(key)
                sources.append(source)
    return sources


def chunk_markdown(markdown: str, size: int = 900) -> list[str]:
    chunks: list[str] = []
    buffer = ""
    in_table = False
    for line in markdown.splitlines(keepends=True):
        is_table_row = line.strip().startswith("|")
        if is_table_row:
            in_table = True
        if len(buffer) + len(line) > size and buffer:
            if in_table and is_table_row:
                pass
            else:
                in_table = False
                chunks.append(buffer)
                buffer = ""
        if not is_table_row and in_table and not line.strip() == "":
            in_table = False
        buffer += line
    if buffer:
        chunks.append(buffer)
    return chunks
