import asyncio
import logging
import re
import time
import uuid
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from ..agent.runner import store
from ..events import sse_format
from .. import storage
from ..templates import DEFAULT_TEMPLATE, normalize_dimensions as normalize_template_dims
from ..tracing import build_trace_from_thinking_steps, get_trace

router = APIRouter()

MAX_TRACK_LENGTH = 200
MAX_COMPETITOR_LENGTH = 100
MAX_COMPETITORS = 20

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9:_\-\.]{1,128}$")


def _client_id(req: Request, query_param: str | None = None) -> str:
    """获取客户端标识：优先 X-Client-Id 头/query，缺省退回 IP。

    用于会话归属校验与历史列表隔离（浏览器维度，无需登录）。
    """
    cid = query_param or req.headers.get("x-client-id") or req.query_params.get("client_id")
    if cid and _CLIENT_ID_RE.match(cid):
        return cid
    ip = req.client.host if req.client else "unknown"
    return f"ip:{ip}"


def _require_owner(session, client_id: str) -> bool:
    """会话归属校验：owner 为空（旧数据）时按 IP 兜底放行。"""
    if not session.owner:
        return True
    return session.owner == client_id

_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 3
_rate_log: dict[str, list[float]] = defaultdict(list)
_last_cleanup: float = 0.0


def _rate_key(request: Request) -> str:
    """限流键：优先取 X-Forwarded-For 首段（反代部署时仍按真实客户端隔离）。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _check_rate_limit(key: str) -> bool:
    global _last_cleanup
    now = time.time()
    # 每 5 分钟清理一次过期 IP 记录，避免内存泄漏
    if now - _last_cleanup > 300:
        _last_cleanup = now
        expired_ips = [ip for ip, ts in _rate_log.items() if now - ts[-1] > _RATE_LIMIT_WINDOW]
        for ip in expired_ips:
            del _rate_log[ip]
    _rate_log[key] = [t for t in _rate_log.get(key, []) if now - t < _RATE_LIMIT_WINDOW]
    if len(_rate_log[key]) >= _RATE_LIMIT_MAX:
        return False
    _rate_log[key].append(now)
    return True


def _sanitize_text(text: str, max_len: int) -> str:
    text = text.strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text[:max_len]


# 搜索来源：已移除「自动」选项，前端必须明确选择具体来源；缺省默认 Tavily。
ALLOWED_PROVIDERS = ("tavily", "bocha", "zhihu", "zhihu_site", "zhida")
DEFAULT_PROVIDER = "tavily"


def _provider_configured(provider: str) -> bool:
    """搜索来源是否已在服务端配置 API Key（zhihu/zhihu_site/zhida 共用 ZHIHU_API_KEY）。"""
    from ..config import get_settings

    s = get_settings()
    if provider == "tavily":
        return bool(s.tavily_api_key)
    if provider == "bocha":
        return bool(s.bocha_api_key)
    return bool(s.zhihu_api_key)


class StartRequest(BaseModel):
    track: str = Field(min_length=1, max_length=MAX_TRACK_LENGTH)
    competitors: list[str] = Field(default_factory=list, max_length=MAX_COMPETITORS)
    depth: Literal["broad", "deep"] | None = None
    search_provider: Literal["tavily", "bocha", "zhihu", "zhihu_site", "zhida"] | None = None
    # 多选搜索来源：一次性按每个来源并行启动一个分析会话（同一窗口分屏对比）
    search_providers: list[Literal["tavily", "bocha", "zhihu", "zhihu_site", "zhida"]] | None = None
    # 分析模版：template_id 引用已保存的默认/自定义模版；template 为内联自定义模版（二选一）
    template_id: str | None = None
    template: dict | None = None


class ConfirmRequest(BaseModel):
    action: Literal["confirm_competitors", "confirm_depth", "confirm_track_intent"]
    data: dict


@router.post("/start")
async def start_analysis(req: StartRequest, request: Request) -> dict:
    if not _check_rate_limit(_rate_key(request)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    track = _sanitize_text(req.track, MAX_TRACK_LENGTH)
    if not track:
        raise HTTPException(status_code=400, detail="赛道不能为空")

    competitors = [_sanitize_text(c, MAX_COMPETITOR_LENGTH) for c in req.competitors if c and c.strip()]
    competitors = [c for c in competitors if c][:MAX_COMPETITORS]

    # 多选来源：search_providers 优先；单值 search_provider 兼容旧前端；缺省 Tavily
    providers = list(req.search_providers or [])
    if not providers and req.search_provider:
        providers = [req.search_provider]
    if not providers:
        providers = [DEFAULT_PROVIDER]

    seen: set[str] = set()
    cleaned: list[str] = []
    for p in providers:
        p = (p or "").strip().lower()
        if p in ALLOWED_PROVIDERS and p not in seen:
            seen.add(p)
            cleaned.append(p)
    if not cleaned:
        cleaned = [DEFAULT_PROVIDER]

    # 未配置 API Key 的来源不允许启动（与前端「置灰不可选」约束一致）：
    # 否则会话会静默跑完但搜索结果为空，浪费一轮 LLM 调用
    unconfigured = [p for p in cleaned if not _provider_configured(p)]
    if unconfigured:
        raise HTTPException(
            status_code=400,
            detail="以下搜索来源未配置 API Key：" + "、".join(unconfigured)
            + "，请选择其他来源或先在服务端完成配置",
        )

    # 解析分析模版：内联 template 优先，其次 template_id（default=内置默认模版）
    template: dict | None = None
    if req.template and req.template.get("dimensions"):
        dims = normalize_template_dims(req.template.get("dimensions"))
        if dims:
            template = {
                "id": str(req.template.get("id") or "") or "custom",
                "name": str(req.template.get("name") or "").strip()[:60] or "自定义模版",
                "is_custom": True,
                "dimensions": dims,
            }
    elif req.template_id:
        tid = (req.template_id or "").strip()
        if tid == "default":
            template = dict(DEFAULT_TEMPLATE)
        else:
            tpl = await storage.aget_template(tid)
            if not tpl:
                raise HTTPException(status_code=404, detail=f"模版不存在：{tid}")
            template = tpl

    client_id = _client_id(request)
    # 多来源分屏：同一批会话共用一个 batch_id，历史侧栏按 batch 只显示一条记录
    batch_id = str(uuid.uuid4()) if len(cleaned) > 1 else ""
    # 每个来源一个独立会话，store.create 内部各自创建 asyncio 任务 → 并行运行
    sessions = []
    for provider in cleaned:
        session = store.create(track, competitors, req.depth, provider, owner=client_id, batch_id=batch_id, template=template)
        sessions.append({"analysis_id": session.id, "search_provider": provider})

    return {
        "status": "started",
        "analysis_id": sessions[0]["analysis_id"],
        "analysis_ids": [s["analysis_id"] for s in sessions],
        "search_providers": cleaned,
        "batch_id": batch_id,
        "template": template,
        "sessions": sessions,
    }


@router.get("/{analysis_id}/stream")
async def stream_analysis(analysis_id: str, request: Request) -> StreamingResponse:
    try:
        session = store.get(analysis_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="analysis not found") from exc

    # 共享模式：所有访问者可见/可操作同一分析
    sub = session.subscribe()
    queue: asyncio.Queue = sub["queue"]

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield sse_format("ping", {})
                    continue
                was_replay = sub["replay_left"] > 0
                if was_replay:
                    sub["replay_left"] -= 1
                yield sse_format(event["event"], event["data"])
                # 断开条件：confirm/done/error 均为终端事件。
                # subscribe() 已保证回放只含“当前正在等待确认”的 confirm（无过期卡片），
                # 因此回放的 confirm 同样应断开，避免连接挂着反复收 ping。
                if event["event"] in {"confirm", "done", "error"}:
                    break
        finally:
            session.unsubscribe(sub)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/{analysis_id}/confirm")
async def confirm_analysis(analysis_id: str, req: ConfirmRequest, request: Request) -> dict[str, str]:
    try:
        session = store.get(analysis_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="analysis not found") from exc

    # 共享模式：不再校验归属
    # 校验确认动作与当前等待确认的类型一致，防止旧确认卡片提交的过期数据
    # 污染后续阶段（例如已越过赛道确认后仍提交 confirm_track_intent，导致死循环）
    _ACTION_TO_TYPE = {
        "confirm_track_intent": "track_intent",
        "confirm_competitors": "competitors",
        "confirm_depth": "depth",
    }
    if not session._interrupted or session.pending_confirm_type is None:
        # 会话不在等待确认状态：拒绝过期/重放的确认，避免取消运行中的任务并注入过期数据
        raise HTTPException(status_code=409, detail="分析当前不在等待确认状态，请刷新页面后重试")
    if _ACTION_TO_TYPE.get(req.action) != session.pending_confirm_type:
        raise HTTPException(
            status_code=409,
            detail=f"确认状态已过期（当前等待：{session.pending_confirm_type}），请刷新页面后重试",
        )
    if req.action == "confirm_competitors":
        confirm_data = {"competitors": req.data.get("competitors", [])}
    elif req.action == "confirm_depth":
        confirm_data = {"depth": req.data.get("depth", "broad")}
    else:
        confirm_data = {"track_intent": req.data.get("track_intent", "")}
    # 先确认成功再记录回复：若 confirm 被拒（resume 进行中等），
    # 不应把未生效的用户回复写入 conversation 污染历史回放
    if not session.confirm(confirm_data):
        # resume 进行中或状态已变化：拒绝重复提交
        raise HTTPException(status_code=409, detail="确认处理中，请稍候再试")
    session.record_user_reply(req.action, req.data)
    return {"status": "confirmed"}


@router.get("/{analysis_id}/cancel")
async def cancel_analysis(analysis_id: str, request: Request) -> dict[str, str]:
    try:
        session = store.get(analysis_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="analysis not found") from exc

    session.cancel()
    return {"status": "cancelled"}


def _group_batches(records: list[dict]) -> list[dict]:
    """把同一 batch 的子会话合并为一条历史记录（多来源分屏一次任务只显示一条）。

    非 batch（batch_id 为空/等于自身 id）原样返回；batch 记录 status 取子会话状态：
    任一 running → running；否则任一 interrupted → interrupted；否则全部 cancelled → cancelled；
    任一 error → error；否则 completed。
    """
    singles: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for r in records:
        bid = r.get("batchId") or ""
        if not bid or bid == r["id"]:
            singles.append(r)
        else:
            groups.setdefault(bid, []).append(r)
    merged = list(singles)
    for bid, group in groups.items():
        group.sort(key=lambda x: x.get("createdAt", ""))
        first = group[0]
        if any(g.get("status") == "running" for g in group):
            status = "running"
        elif any(g.get("status") == "interrupted" for g in group):
            status = "interrupted"
        elif all(g.get("status") == "cancelled" for g in group):
            status = "cancelled"
        elif any(g.get("status") == "error" for g in group):
            status = "error"
        else:
            status = "completed"
        merged.append({
            "id": bid,
            "batchId": bid,
            "track": first.get("track", ""),
            "competitors": first.get("competitors", []),
            "depth": first.get("depth", "broad"),
            "reportMd": "",
            "sources": [],
            "researchPlan": None,
            "synthesis": None,
            "evaluation": None,
            "conversation": [],
            "status": status,
            "clientId": "",
            "searchProvider": first.get("searchProvider", "auto"),
            "searchProviders": [g.get("searchProvider", "auto") for g in group],
            "analysisIds": [g["id"] for g in group],
            "createdAt": first.get("createdAt", ""),
            "batch": True,
        })
    merged.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    return merged


@router.get("/history/list")
async def list_history(limit: int = 100, request: Request = None) -> list[dict]:
    # 共享模式：返回全部历史，不做 client_id 隔离
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit 取值必须在 1-200 之间")
    records = await storage.alist_analyses(limit=limit)
    return _group_batches(records)


@router.post("/history/cleanup")
async def cleanup_history(request: Request) -> dict:
    """一键清理全部「已中断」记录（含整批）。

    用于清理由异常中断/历史崩坏产生的残留重复脏数据。
    """
    try:
        deleted, batches = await storage.adelete_interrupted()
        # 同步清理内存中仍持有的对应会话
        store._cleanup_interrupted()
        return {"status": "ok", "deleted": deleted, "batches": batches}
    except Exception as exc:
        logger.exception("Failed to cleanup interrupted history")
        raise HTTPException(status_code=500, detail=f"清理失败：{exc}") from exc


@router.get("/history/{analysis_id}")
async def get_history(analysis_id: str, request: Request) -> dict:
    record = await storage.aget_analysis(analysis_id)
    if record:
        return record
    # 非真实会话 id：可能是多来源 batch_id，返回聚合记录 + 各子会话完整信息（供报告弹窗按来源切换）
    children = await storage.alist_analyses_by_batch(analysis_id, limit=100)
    if children:
        merged = _group_batches(children)
        if merged:
            batch = merged[0]
            batch["children"] = children
            return batch
    raise HTTPException(status_code=404, detail="history not found")


@router.delete("/history/{analysis_id}")
async def delete_history(analysis_id: str, request: Request) -> dict[str, str]:
    """删除历史记录：任意状态均可删除；若为运行中，先终止对应会话再删除。

    返回明确的成功/失败信息，前端据此展示 toast。
    """
    try:
        record = await storage.aget_analysis(analysis_id)
        if not record:
            # 多来源 batch：删除其全部子会话（运行中的先终止）
            children = await storage.alist_analyses_by_batch(analysis_id, limit=100)
            if not children:
                raise HTTPException(status_code=404, detail="history not found")
            for c in children:
                if c.get("status") == "running":
                    store.drop(c["id"])
            await storage.adelete_analyses_by_batch(analysis_id)
            return {"status": "deleted", "detail": "已删除"}
        # 单会话（含 batch 内单条）：共享模式，任何访问者均可删除
        # 删除运行中的记录时同时终止对应会话
        if record.get("status") == "running":
            store.drop(analysis_id)
        deleted = await storage.adelete_analysis(analysis_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="history not found")
        return {"status": "deleted", "detail": "已删除"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to delete history %s", analysis_id)
        raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc


@router.get("/{analysis_id}/trace")
async def get_analysis_trace(analysis_id: str, request: Request) -> dict:
    """获取分析的完整执行链路（Tracing）。

    返回各 Agent 节点的执行时间、Token 消耗、成本估算等信息。
    """
    # 先尝试从内存中获取实时 trace
    trace = get_trace(analysis_id)
    if trace:
        return trace.to_dict()

    # 否则从历史记录构建 trace
    record = await storage.aget_analysis(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="analysis not found")

    # 从 thinking_steps 构建 trace
    thinking_steps = record.get("thinkingSteps", [])
    if not thinking_steps:
        # 如果没有 thinking_steps，返回基本信息
        return {
            "analysis_id": analysis_id,
            "track": record.get("track", ""),
            "total_latency_ms": 0,
            "total_tokens": 0,
            "status": "completed",
            "spans": [],
            "cost_estimate": {"input_cost_yuan": 0, "output_cost_yuan": 0, "total_cost_yuan": 0},
            "token_distribution": {},
        }

    trace = build_trace_from_thinking_steps(
        analysis_id=analysis_id,
        track=record.get("track", ""),
        thinking_steps=thinking_steps,
        total_elapsed_seconds=0,
    )
    return trace.to_dict()
