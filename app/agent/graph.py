import asyncio
import logging
import time
from dataclasses import asdict
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Send

from .analyze_agent import analyze_competitors
from .llm import get_accumulated_tokens, reset_token_tracker
from .reflection_agent import reflect_on_research
from .report_agent import build_report, chunk_markdown, collect_sources
from .research_planner import plan_research
from .search_agent import collect_competitor, discover_competitors, plan_competitor_discovery
from .state import AnalysisResult, CompetitorInfo, Depth, Evidence, MAX_RESEARCH_ROUNDS, Source
from .synthesize_agent import synthesize_analysis
from ..evaluation.evaluator import evaluate_analysis
from ..memory.retriever import retrieve_relevant_memories
from ..memory.store import get_memory_store

logger = logging.getLogger(__name__)


def _merge_research(left: dict, right: dict) -> dict:
    return {**left, **right}


def _merge_list(left: list, right: list) -> list:
    return left + right


def _overwrite(left: Any, right: Any) -> Any:
    return right


class GraphState(TypedDict):
    track: str
    competitors: list[str]
    depth: str
    search_provider: str
    template: dict | None

    research_plan: dict
    discovery_done: bool
    discovery_competitors: list[dict[str, str]]
    discovery_planned: bool
    pending_track_clarification: list[dict[str, str]]

    research_round: int
    reflected: Annotated[bool, _overwrite]
    research_gaps: Annotated[list[dict[str, Any]], _overwrite]

    research_data: Annotated[dict, _merge_research]
    analysis_result: dict | None
    synthesis: dict | None

    report_md: str
    all_sources: list[dict[str, str]]
    done: bool
    evaluation_result: dict | None

    pending_confirmation: dict | None
    confirmation_result: dict | None

    progress_events: Annotated[list[dict], _merge_list]
    chunk_events: Annotated[list[dict], _merge_list]
    thinking_steps: Annotated[list[dict], _merge_list]


def _initial_state(
    track: str,
    competitors: list[str],
    depth: Depth | None,
    search_provider: str = "tavily",
    template: dict | None = None,
) -> dict:
    # 广度和深度已合并：不再让用户选择广度/深度，统一按完整（含反思补采）模式执行。
    # depth 保留为内部字段（兼容历史记录/旧调用方），默认 deep。
    return {
        "track": track,
        "competitors": competitors,
        "depth": depth or "deep",
        "search_provider": search_provider,
        "template": template,
        "research_plan": {},
        "discovery_done": False,
        "discovery_competitors": [],
        "discovery_planned": False,
        "pending_track_clarification": [],
        "research_round": 0,
        "reflected": False,
        "research_gaps": [],
        "research_data": {},
        "analysis_result": None,
        "synthesis": None,
        "report_md": "",
        "all_sources": [],
        "done": False,
        "evaluation_result": None,
        "pending_confirmation": None,
        "confirmation_result": None,
        "progress_events": [],
        "chunk_events": [],
        "thinking_steps": [],
    }


async def supervisor(state: GraphState) -> dict:
    competitors = state["competitors"]
    discovery_done = state["discovery_done"]
    research_plan = state.get("research_plan", {})
    research_data = state["research_data"]
    analysis_result = state["analysis_result"]
    report_md = state["report_md"]
    research_round = state.get("research_round", 0)
    reflected = state.get("reflected", False)
    research_gaps = state.get("research_gaps", [])
    confirmation_result = state.get("confirmation_result")

    if confirmation_result:
        confirm_type = state.get("pending_confirmation", {}).get("type", "")
        updates: dict[str, Any] = {"pending_confirmation": None, "confirmation_result": None}
        if confirm_type == "track_intent":
            track_intent = str(confirmation_result.get("track_intent", "")).strip()
            if track_intent:
                updates["track"] = f"{state['track']} - {track_intent}"
            updates["pending_track_clarification"] = []
        elif confirm_type == "competitors":
            updates["competitors"] = [str(item).strip() for item in confirmation_result.get("competitors", []) if str(item).strip()]
        return updates

    if not research_plan:
        return {"progress_events": [{"step": "正在制定研究计划", "agent": "planner", "progress": "0/7"}]}

    pending_clarification = state.get("pending_track_clarification", [])
    if pending_clarification and not confirmation_result:
        return {"pending_confirmation": {
            "type": "track_intent",
            "question": "请确认你要分析的细分赛道",
            "options": [item.get("label", "") for item in pending_clarification if item.get("label")],
            "briefs": [
                {"name": item.get("label", ""), "brief": item.get("description", "")}
                for item in pending_clarification
                if item.get("label")
            ],
        }}

    if not competitors and not discovery_done:
        return {"progress_events": [{"step": "正在判断赛道边界并发现竞品", "agent": "search", "progress": "1/7"}]}

    if not competitors and discovery_done and state["discovery_competitors"]:
        return {"pending_confirmation": {
            "type": "competitors",
            "question": "请确认竞品列表",
            "options": [item["name"] for item in state["discovery_competitors"]],
            "briefs": state["discovery_competitors"],
        }}

    if not research_data:
        return {"progress_events": [{"step": f"正在并行采集竞品信息（第{research_round + 1}轮）", "agent": "search", "progress": f"2/7"}]}

    if research_data and not reflected and research_round < MAX_RESEARCH_ROUNDS:
        # 广度/深度已合并：统一执行反思补采，保证信息完整度
        return {"progress_events": [{"step": "正在评估信息完整度，识别证据缺口", "agent": "reflect", "progress": "3/7"}]}

    if reflected and research_gaps and research_round < MAX_RESEARCH_ROUNDS:
        gap_names = [g.get("competitor", "") for g in research_gaps[:3]]
        return {"progress_events": [{"step": f"正在针对缺口补采：{', '.join(gap_names)}", "agent": "search", "progress": "3/7"}]}

    if not analysis_result:
        return {"progress_events": [{"step": "正在并行生成对比分析与综合洞察", "agent": "analyze", "progress": "4/7"}]}

    if not report_md:
        return {"progress_events": [{"step": "正在生成 Markdown 报告", "agent": "report", "progress": "6/7"}]}

    return {}


def supervisor_router(state: GraphState):
    competitors = state["competitors"]
    discovery_done = state["discovery_done"]
    depth = state["depth"]
    research_plan = state.get("research_plan", {})
    research_data = state["research_data"]
    analysis_result = state["analysis_result"]
    report_md = state["report_md"]
    research_round = state.get("research_round", 0)
    reflected = state.get("reflected", False)
    research_gaps = state.get("research_gaps", [])
    pending_confirmation = state.get("pending_confirmation")
    confirmation_result = state.get("confirmation_result")

    if pending_confirmation and not confirmation_result:
        return "end"

    if confirmation_result:
        return "supervisor"

    if not research_plan:
        return "research_plan"

    pending_clarification = state.get("pending_track_clarification", [])
    if pending_clarification:
        return "supervisor"

    if not competitors and not discovery_done:
        return "search_plan"
    if not competitors and discovery_done and state["discovery_competitors"]:
        return "supervisor"
    if not research_data:
        return [Send("collect", {
            "track": state["track"],
            "competitor_name": name,
            "depth": depth,
            "index": i,
            "total": len(competitors),
            "round": research_round,
            "research_plan": research_plan,
            "search_provider": state.get("search_provider", "tavily"),
        }) for i, name in enumerate(competitors)]

    if research_data and not reflected and research_round < MAX_RESEARCH_ROUNDS:
        # 广度/深度已合并：统一走反思与补采
        return "reflect"

    if reflected and research_gaps and research_round < MAX_RESEARCH_ROUNDS:
        # 限制最多补采 3 个缺口
        gaps_to_fill = [g for g in research_gaps[:3] if g.get("competitor")]
        return [Send("collect", {
            "track": state["track"],
            "competitor_name": gap.get("competitor", ""),
            "depth": depth,
            "index": i,
            "total": len(gaps_to_fill),
            "round": research_round,
            "extra_queries": gap.get("followup_queries", []),
            "is_gap_fill": True,
            "existing_research_data": research_data.get(gap.get("competitor", "")),
            "research_plan": research_plan,
            "search_provider": state.get("search_provider", "tavily"),
        }) for i, gap in enumerate(gaps_to_fill)]

    if not analysis_result:
        return "analysis"
    if not report_md:
        return "report"
    return "end"


async def search_plan_node(state: GraphState) -> dict:
    track = state["track"]
    competitors = state["competitors"]
    discovery_done = state["discovery_done"]
    discovery_planned = state["discovery_planned"]
    search_provider = state.get("search_provider", "tavily")

    if competitors or discovery_done:
        return {}

    reset_token_tracker()
    t0 = time.perf_counter()

    if not discovery_planned:
        plan = await plan_competitor_discovery(track)
        interpretations = plan.get("interpretations", [])

        if plan.get("needs_clarification") and len(interpretations) > 1:
            elapsed = round((time.perf_counter() - t0) * 1000)
            tokens = get_accumulated_tokens()
            return {
                "discovery_planned": True,
                "pending_track_clarification": interpretations,
                "progress_events": [{"step": "赛道含糊，等待用户确认细分方向", "agent": "search", "progress": "2/7", "elapsed_ms": elapsed, "tokens": tokens.total_tokens}],
                "thinking_steps": [{"agent": "search", "step": "赛道意图判断", "elapsed_ms": elapsed, "tokens": tokens.total_tokens, "detail": "赛道含多种解读，待用户确认"}],
            }

    discovered = await discover_competitors(track, search_provider=search_provider)

    elapsed = round((time.perf_counter() - t0) * 1000)
    tokens = get_accumulated_tokens()
    return {
        "track": track,
        "discovery_done": True,
        "discovery_planned": True,
        "pending_track_clarification": [],
        "discovery_competitors": discovered,
        "progress_events": [{"step": "已推荐候选竞品", "agent": "search", "progress": "2/7", "elapsed_ms": elapsed, "tokens": tokens.total_tokens}],
        "thinking_steps": [{"agent": "search", "step": "竞品发现与推荐", "elapsed_ms": elapsed, "tokens": tokens.total_tokens, "detail": f"发现 {len(discovered)} 个候选竞品"}],
    }


async def research_plan_node(state: GraphState) -> dict:
    if state.get("research_plan"):
        return {}

    reset_token_tracker()
    t0 = time.perf_counter()

    # 检索长期记忆，注入历史分析经验
    memory_context = retrieve_relevant_memories(
        track=state["track"],
        competitors=state.get("competitors") or None,
    )

    plan = await plan_research(state["track"], memory_context=memory_context, template=state.get("template"))

    elapsed = round((time.perf_counter() - t0) * 1000)
    tokens = get_accumulated_tokens()
    return {
        "research_plan": plan,
        "progress_events": [{"step": "研究计划已制定", "agent": "planner", "progress": "1/7", "elapsed_ms": elapsed, "tokens": tokens.total_tokens}],
        "thinking_steps": [{"agent": "planner", "step": "制定研究计划", "elapsed_ms": elapsed, "tokens": tokens.total_tokens, "detail": f"规划 {len(plan.get('dimensions', []))} 个研究维度" + ("（已注入历史记忆）" if memory_context else "")}],
    }


async def collect_node(state: dict) -> dict:
    track = state["track"]
    name = state["competitor_name"]
    depth = state["depth"]
    index = state["index"]
    total = state["total"]
    extra_queries = state.get("extra_queries")
    is_gap_fill = state.get("is_gap_fill", False)
    search_provider = state.get("search_provider", "tavily")

    existing_info = None
    if is_gap_fill:
        existing_data = state.get("existing_research_data")
        if existing_data and isinstance(existing_data, dict):
            existing_info = _reconstruct_one(name, existing_data)

    reset_token_tracker()
    t0 = time.perf_counter()

    research_plan = state.get("research_plan")
    info = await collect_competitor(track, name, depth, extra_queries=extra_queries, existing_info=existing_info, research_plan=research_plan, search_provider=search_provider)

    elapsed = round((time.perf_counter() - t0) * 1000)
    tokens = get_accumulated_tokens()

    label = "补采完成" if is_gap_fill else "已采集"
    result = {
        "research_data": {name: info.to_dict()},
        "progress_events": [{
            "step": f"{label} {name} 信息",
            "agent": "search",
            "competitor": name,
            "progress": f"{index + 1}/{total}",
            "elapsed_ms": elapsed,
            "tokens": tokens.total_tokens,
        }],
        "thinking_steps": [{"agent": "search", "step": f"{label} {name}", "elapsed_ms": elapsed, "tokens": tokens.total_tokens, "detail": f"采集 {len(info.evidences)} 条证据"}],
    }
    if is_gap_fill:
        result["reflected"] = False
        result["research_gaps"] = []
    return result


async def reflect_node(state: GraphState) -> dict:
    competitors = state["competitors"]
    depth = state["depth"]
    research_plan = state.get("research_plan", {})
    research_data = state["research_data"]
    research_round = state.get("research_round", 0)

    infos = _reconstruct_infos(competitors, research_data)

    reset_token_tracker()
    t0 = time.perf_counter()

    result = await reflect_on_research(state["track"], depth, research_plan, infos, research_round)

    elapsed = round((time.perf_counter() - t0) * 1000)
    tokens = get_accumulated_tokens()

    ready = result.get("ready_for_analysis", True)
    gaps = result.get("gaps", [])
    assessment = result.get("overall_assessment", "")

    new_round = research_round + 1 if gaps else research_round

    return {
        "reflected": True,
        "research_gaps": gaps if not ready else [],
        "research_round": new_round,
        "progress_events": [{
            "step": f"反思完成：{assessment}" if assessment else "反思完成",
            "agent": "reflect",
            "progress": "3/7",
            "gaps_found": len(gaps) if not ready else 0,
            "elapsed_ms": elapsed,
            "tokens": tokens.total_tokens,
        }],
        "thinking_steps": [{"agent": "reflect", "step": "反思评估信息完整度", "elapsed_ms": elapsed, "tokens": tokens.total_tokens, "detail": f"发现 {len(gaps)} 个信息缺口" if gaps else "信息充分，无需补采"}],
    }


async def analysis_node(state: GraphState) -> dict:
    """并行执行结构化对比分析与综合洞察生成。

    综合阶段使用确定性的初步分析数据，不依赖 LLM 分析结果，
    两个 LLM 调用并发进行，整体耗时 = max(分析, 综合) 而非两者之和。
    """
    competitors = state["competitors"]
    depth = state["depth"]
    research_data = state["research_data"]
    research_plan = state.get("research_plan", {})

    infos = _reconstruct_infos(competitors, research_data)

    reset_token_tracker()
    t0 = time.perf_counter()

    progress_events: list[dict[str, Any]] = []
    thinking_steps: list[dict[str, Any]] = []

    def _on_done(kind: str) -> None:
        elapsed = round((time.perf_counter() - t0) * 1000)
        tokens = get_accumulated_tokens()
        if kind == "analyze":
            progress_events.append({"step": "结构化对比分析完成", "agent": "analyze", "progress": "4/7", "elapsed_ms": elapsed, "tokens": tokens.total_tokens})
            thinking_steps.append({"agent": "analyze", "step": "结构化对比分析", "elapsed_ms": elapsed, "tokens": tokens.total_tokens, "detail": f"对比 {len(infos)} 个竞品"})
        else:
            progress_events.append({"step": "综合分析完成，已生成洞察和建议", "agent": "synthesize", "progress": "5/7", "elapsed_ms": elapsed, "tokens": tokens.total_tokens})
            thinking_steps.append({"agent": "synthesize", "step": "综合分析与洞察生成", "elapsed_ms": elapsed, "tokens": tokens.total_tokens, "detail": "生成执行摘要、关键洞察和战略建议"})

    tasks: dict[str, asyncio.Task] = {
        "analyze": asyncio.create_task(analyze_competitors(state["track"], infos, depth)),
        "synthesize": asyncio.create_task(synthesize_analysis(state["track"], depth, research_plan, infos)),
    }
    for kind, task in tasks.items():
        task.add_done_callback(lambda _done, k=kind: _on_done(k))
    await asyncio.gather(*tasks.values())

    def _safe_result(task: asyncio.Task) -> Any:
        if task.cancelled():
            return None
        try:
            return task.result()
        except Exception:
            logger.exception("分析/综合阶段失败，使用确定性兜底结果")
            return None

    updates: dict[str, Any] = {"progress_events": progress_events, "thinking_steps": thinking_steps}
    analysis = _safe_result(tasks["analyze"])
    if analysis is not None:
        updates["analysis_result"] = {
            "positioning_rows": analysis.positioning_rows,
            "feature_matrix": analysis.feature_matrix,
            "evaluation_dimensions": analysis.evaluation_dimensions,
            "scorecard_rows": analysis.scorecard_rows,
            "capability_rows": analysis.capability_rows,
            "battlecard_rows": analysis.battlecard_rows,
            "opportunity_rows": analysis.opportunity_rows,
            "pricing_rows": analysis.pricing_rows,
            "user_rows": analysis.user_rows,
            "swot": analysis.swot,
            "tech_rows": analysis.tech_rows,
            "sentiment_rows": analysis.sentiment_rows,
            "insights": analysis.insights,
        }
    synthesis = _safe_result(tasks["synthesize"])
    if synthesis is not None:
        updates["synthesis"] = synthesis
    return updates


async def report_node(state: GraphState) -> dict:
    competitors = state["competitors"]
    depth = state["depth"]
    research_data = state["research_data"]
    analysis_data = state["analysis_result"]
    synthesis_data = state.get("synthesis")
    research_plan = state.get("research_plan", {})
    research_round = state.get("research_round", 0)
    thinking_steps = state.get("thinking_steps", [])

    infos = _reconstruct_infos(competitors, research_data)
    analysis = AnalysisResult(**analysis_data)

    t0 = time.perf_counter()

    report_md = build_report(
        state["track"], depth, infos, analysis,
        synthesis=synthesis_data,
        research_plan=research_plan,
    )

    chunks = [{"content": chunk} for chunk in chunk_markdown(report_md)]

    sources = collect_sources(infos)
    all_sources = [asdict(s) for s in sources]

    elapsed = round((time.perf_counter() - t0) * 1000)

    # --- Agent 性能评估（EDD 评估驱动开发）---
    total_tokens = sum(s.get("tokens", 0) for s in thinking_steps)
    total_latency = sum(s.get("elapsed_ms", 0) for s in thinking_steps) / 1000.0
    eval_result = evaluate_analysis(
        research_plan=research_plan,
        research_data=research_data,
        report_md=report_md,
        thinking_steps=thinking_steps,
        latency_seconds=total_latency,
        total_tokens=total_tokens,
        research_rounds=research_round,
    )

    return {
        "report_md": report_md,
        "all_sources": all_sources,
        "done": True,
        "evaluation_result": eval_result.to_dict(),
        "chunk_events": chunks,
        "progress_events": [{"step": "报告生成完成", "agent": "report", "progress": "7/7", "elapsed_ms": elapsed, "tokens": 0}],
        "thinking_steps": [{"agent": "report", "step": "生成 Markdown 报告", "elapsed_ms": elapsed, "tokens": 0, "detail": f"报告共 {len(report_md)} 字"}],
    }


def _reconstruct_infos(competitors: list[str], research_data: dict) -> list[CompetitorInfo]:
    infos: list[CompetitorInfo] = []
    for name in competitors:
        data = research_data.get(name, {})
        if isinstance(data, dict):
            infos.append(_reconstruct_one(name, data))
    return infos


def _reconstruct_one(name: str, data: dict) -> CompetitorInfo:
    sources = [
        Source(
            label=str(s.get("label", "")),
            url=str(s.get("url", "")),
            type=str(s.get("type", "llm_knowledge")),
        )
        for s in data.get("sources", [])
        if isinstance(s, dict)
    ]
    evidences = [
        Evidence(
            id=str(e.get("id", "")),
            competitor=str(e.get("competitor", name)),
            source_type=str(e.get("source_type", "llm_knowledge")),
            title=str(e.get("title", "")),
            url=str(e.get("url", "")),
            snippet=str(e.get("snippet", "")),
            credibility=str(e.get("credibility", "low")),
            collected_at=str(e.get("collected_at", "")),
        )
        for e in data.get("evidences", [])
        if isinstance(e, dict)
    ]
    return CompetitorInfo(
        name=data.get("name", name),
        features=data.get("features", []),
        capabilities=data.get("capabilities", []),
        pricing=data.get("pricing", ""),
        pricing_freetier=data.get("pricing_freetier", ""),
        pricing_paid_min=data.get("pricing_paid_min", ""),
        target_users=data.get("target_users", ""),
        positioning=data.get("positioning", ""),
        key_selling_points=data.get("key_selling_points", []),
        differentiation=data.get("differentiation", ""),
        tech_stack=data.get("tech_stack", ""),
        api_openness=data.get("api_openness", ""),
        strengths=data.get("strengths", []),
        weaknesses=data.get("weaknesses", []),
        sentiment=data.get("sentiment", ""),
        evidence_notes=data.get("evidence_notes", []),
        evidences=evidences,
        sources=sources,
    )


def build_graph():
    builder = StateGraph(GraphState)

    builder.add_node("supervisor", supervisor)
    builder.add_node("research_plan", research_plan_node)
    builder.add_node("search_plan", search_plan_node)
    builder.add_node("collect", collect_node)
    builder.add_node("reflect", reflect_node)
    builder.add_node("analysis", analysis_node)
    builder.add_node("report", report_node)

    builder.set_entry_point("supervisor")

    builder.add_conditional_edges(
        "supervisor",
        supervisor_router,
        {
            "research_plan": "research_plan",
            "search_plan": "search_plan",
            "collect": "collect",
            "reflect": "reflect",
            "analysis": "analysis",
            "report": "report",
            "supervisor": "supervisor",
            "end": END,
        },
    )

    builder.add_edge("research_plan", "supervisor")
    builder.add_edge("search_plan", "supervisor")
    builder.add_edge("collect", "supervisor")
    builder.add_edge("reflect", "supervisor")
    builder.add_edge("analysis", "supervisor")
    builder.add_edge("report", END)

    return builder


def compile_graph(saver):
    """用已 enter 的 checkpointer 编译图。saver 生命周期由调用方管理。"""
    return build_graph().compile(checkpointer=saver)


_graph: Any = None


def set_graph(graph) -> None:
    global _graph
    _graph = graph


async def get_graph():
    if _graph is None:
        raise RuntimeError("graph not initialized; lifespan must set_graph() first")
    return _graph
