import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .graph import get_graph, _initial_state
from .state import Depth
from ..events import StreamEvent
from .. import storage
from ..memory.store import get_memory_store

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 3600
MAX_SESSIONS = 200

# 分析执行超时预算（秒）：基础 + 每竞品 + 深模式附加，封顶 20 分钟
_TIMEOUT_BASE = 360
_TIMEOUT_PER_COMPETITOR = 60
_TIMEOUT_DEEP_BONUS = 300
_TIMEOUT_CAP = 1200


def compute_analysis_timeout(depth: Depth | None, competitor_count: int) -> int:
    budget = _TIMEOUT_BASE + _TIMEOUT_PER_COMPETITOR * max(competitor_count, 1)
    if depth == "deep":
        budget += _TIMEOUT_DEEP_BONUS
    return min(budget, _TIMEOUT_CAP)


# 事件回放缓冲上限：新连接/重连时先回放这些事件（丢弃 ping）
_REPLAY_MAX = 500


class AnalysisSession:
    def __init__(self, track: str, competitors: list[str], depth: Depth | None, search_provider: str = "tavily",
                 owner: str = "", session_id: str | None = None, created_at_iso: str | None = None,
                 conversation: list[dict[str, Any]] | None = None, batch_id: str = "",
                 template: dict[str, Any] | None = None):
        self.id = session_id or str(uuid.uuid4())
        self.thread_id = f"analysis-{self.id}"
        self.owner = owner
        self.track = track
        self.competitors = competitors
        self.depth = depth
        self.search_provider = search_provider or "tavily"
        self.template = template
        self.batch_id = batch_id or self.id
        # 每个 SSE 连接一个独立订阅队列，事件广播到所有订阅者（修复多连接瓜分）
        self.subscribers: list[asyncio.Queue[StreamEvent]] = []
        # 事件回放缓冲：重连/迟到的连接可拿到历史事件
        self.replay: list[StreamEvent] = []
        self.task: asyncio.Task[None] | None = None
        self.cancelled = False
        self.report_md = ""
        self.created_at = time.monotonic()
        self.created_at_iso = created_at_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.deadline = self.created_at + compute_analysis_timeout(depth, len(competitors))
        self._interrupted = False
        self._finished = False
        # resume 进行中标志：防止连续/重复 confirm 重复注入图状态
        self._resuming = False
        # 当前正在等待用户确认的类型（track_intent/competitors/depth），用于校验确认动作是否过期
        self.pending_confirm_type: str | None = None
        self._emitted_progress = 0
        self._emitted_chunks = 0
        self._emitted_thinking = 0
        self.conversation: list[dict[str, Any]] = list(conversation or [])
        if not self.conversation:
            self._record_user_request(track, competitors, depth, template)

    @classmethod
    def reconstruct(cls, record: dict[str, Any]) -> "AnalysisSession":
        """从持久化记录重建会话（进程重启后恢复 running 会话用）。"""
        depth = record.get("depth") if record.get("depth") in {"broad", "deep"} else None
        return cls(
            track=record.get("track", ""),
            competitors=record.get("competitors", []),
            depth=depth,
            search_provider=record.get("searchProvider", "tavily"),
            owner=record.get("clientId", ""),
            session_id=record["id"],
            created_at_iso=record.get("createdAt"),
            conversation=record.get("conversation", []),
            batch_id=record.get("batchId", "") or record["id"],
        )

    def _record(self, entry: dict[str, Any]) -> None:
        entry = dict(entry)
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.conversation.append(entry)

    def _record_user_request(self, track: str, competitors: list[str], depth: Depth | None,
                             template: dict[str, Any] | None = None) -> None:
        parts = [f"赛道：{track}"]
        if competitors:
            parts.append(f"竞品：{'、'.join(competitors)}")
        if template and template.get("name"):
            parts.append(f"模版：{template['name']}")
        self._record({
            "role": "user",
            "type": "text",
            "content": " / ".join(parts),
            "request": {"track": track, "competitors": competitors, "depth": depth, "template": template},
        })

    def record_user_reply(self, action: str, data: dict[str, Any]) -> None:
        """记录用户在确认环节的回复，供历史对话回放使用。"""
        if action == "confirm_competitors":
            content = f"确认竞品：{data.get('competitors', [])}"
        elif action == "confirm_depth":
            content = f"分析深度：{'深度优先' if data.get('depth') == 'deep' else '广度优先'}"
        elif action == "confirm_track_intent":
            content = f"细分赛道：{data.get('track_intent', '')}"
        else:
            content = ""
        if not content:
            return
        self._record({"role": "user", "type": "text", "content": content})

    async def emit(self, event: StreamEvent) -> None:
        """广播事件给所有订阅者，并写入回放缓冲。"""
        if event["event"] != "ping":
            self.replay.append(event)
            if len(self.replay) > _REPLAY_MAX:
                self.replay = self.replay[-_REPLAY_MAX:]
        slow = []
        for sub in self.subscribers:
            try:
                sub["queue"].put_nowait(event)
            except asyncio.QueueFull:
                slow.append(sub)
        for sub in slow:
            # 先通知慢订阅者已被断开，再移除，避免其连接变成“只收 ping”的空连接
            try:
                sub["queue"].put_nowait({
                    "event": "error",
                    "data": {"message": "连接接收过慢已被服务器断开，请刷新页面重试", "retryable": True},
                })
            except asyncio.QueueFull:
                pass
            self.subscribers.remove(sub)

    def subscribe(self) -> dict:
        """新连接订阅：先回放缓冲内事件，再实时广播。

        返回 {queue, replay_left}。
        回放规则（只回放“当前状态”，历史均为过期数据）：
        - confirm  事件只回放“当前正在等待确认”的那一条（最新的那条），
          否则前端会收到旧确认卡片并反复确认，造成死循环；
        - progress/thinking 事件只回放最新的一条（当前进度/当前思考），
          否则确认细分赛道/竞品后重连会把最早的“正在制定研究计划”等
          历史步骤重新渲染一遍，界面看起来像从头开始。
        """
        latest_idx = {kind: -1 for kind in ("confirm", "progress", "thinking")}
        for idx, event in enumerate(self.replay):
            kind = event["event"]
            if kind in latest_idx:
                latest_idx[kind] = idx

        sub: dict = {"queue": asyncio.Queue(maxsize=_REPLAY_MAX), "replay_left": 0}
        for idx, event in enumerate(self.replay):
            kind = event["event"]
            if kind == "ping":
                continue
            if kind == "confirm":
                # 只在会话确实在等待确认时回放最新一条 confirm，其余一律跳过
                if not self._interrupted or idx != latest_idx["confirm"]:
                    continue
            if kind in ("progress", "thinking") and idx != latest_idx[kind]:
                continue
            sub["queue"].put_nowait(event)
            sub["replay_left"] += 1
        self.subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: dict) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)

    async def run(self) -> None:
        logger.info("Starting analysis %s for track '%s'", self.id, self.track)
        try:
            await self._run_graph()
        except asyncio.CancelledError:
            await self._mark_cancelled()
        except Exception as exc:
            logger.exception("Analysis %s failed", self.id)
            await storage.aupdate_analysis_status(self.id, "error")
            await self.emit({"event": "error", "data": {"message": str(exc), "retryable": True}})

    async def _invoke_graph(self, input_data, config: dict) -> dict:
        graph = await get_graph()
        return await graph.ainvoke(input_data, config)

    async def _get_state(self, config: dict):
        graph = await get_graph()
        return await graph.aget_state(config)

    async def _run_graph(self) -> None:
        config = {"configurable": {"thread_id": self.thread_id}}
        initial = _initial_state(self.track, self.competitors, self.depth, self.search_provider, self.template)

        while not self._finished and not self.cancelled:
            self._interrupted = False

            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                await storage.aupdate_analysis_status(self.id, "error")
                await self.emit({"event": "error", "data": {"message": "分析超时，已自动终止", "retryable": True}})
                return

            try:
                result = await asyncio.wait_for(self._invoke_graph(initial, config), timeout=remaining)
            except asyncio.TimeoutError:
                await storage.aupdate_analysis_status(self.id, "error")
                await self.emit({"event": "error", "data": {"message": "分析超时，已自动终止", "retryable": True}})
                return

            await self._emit_events(result)

            pending = result.get("pending_confirmation")
            if pending and not result.get("confirmation_result"):
                self._interrupted = True
                self.pending_confirm_type = pending.get("type")
                await self.emit({"event": "confirm", "data": pending})
                self._record({"role": "agent", "type": "confirm", "confirm": pending})
                return

            if result.get("done"):
                await self._complete(result)
                return

            initial = None

    async def _complete(self, result: dict) -> None:
        """分析完成：记录对话消息、持久化并发送 done 事件。"""
        self._finished = True
        self._interrupted = False
        self.pending_confirm_type = None
        self.report_md = result.get("report_md", "")
        sources = result.get("all_sources", [])
        research_plan = result.get("research_plan", {})
        synthesis = result.get("synthesis")
        thinking_steps = result.get("thinking_steps", [])
        evaluation_result = result.get("evaluation_result")
        total_tokens = sum(s.get("tokens", 0) for s in thinking_steps)
        elapsed = round(time.monotonic() - self.created_at, 1)
        # 记录报告消息与完成摘要，供历史对话回放使用
        if self.report_md:
            self._record({"role": "agent", "type": "report", "reportMd": self.report_md})
        self._record({
            "role": "agent",
            "type": "text",
            "content": f"分析完成，耗时 {elapsed} 秒，消耗 {total_tokens} tokens",
        })
        await self._persist(sources, research_plan, synthesis, evaluation=evaluation_result)
        await self.emit({
            "event": "done",
            "data": {
                "analysis_id": self.id,
                "report_md": self.report_md,
                "sources": sources,
                "track": result.get("track", self.track),
                "competitors": result.get("competitors", self.competitors),
                "depth": result.get("depth", self.depth or "deep"),
                "template": self.template,
                "research_plan": research_plan,
                "synthesis": synthesis,
                "elapsed_seconds": elapsed,
                "thinking_steps": thinking_steps,
                "total_tokens": total_tokens,
                "evaluation": evaluation_result,
            },
        })
        logger.info("Analysis %s completed in %ss", self.id, elapsed)

    async def _persist(self, sources: list[dict[str, str]], research_plan: dict | None = None, synthesis: dict | None = None, status: str = "completed", evaluation: dict | None = None) -> None:
        depth = self.depth or "broad"
        try:
            await storage.asave_analysis(
                analysis_id=self.id,
                track=self.track,
                competitors=self.competitors,
                depth=depth,
                report_md=self.report_md,
                sources=sources,
                created_at=self.created_at_iso,
                research_plan=research_plan or {},
                synthesis=synthesis,
                conversation=self.conversation,
                status=status,
                client_id=self.owner,
                evaluation=evaluation,
                search_provider=self.search_provider,
                batch_id=self.batch_id,
            )
        except Exception:
            logger.exception("Failed to persist analysis %s", self.id)

        # 将分析经验沉淀到长期记忆
        try:
            memory_store = get_memory_store()
            key_insights = []
            if synthesis:
                key_insights = synthesis.get("key_insights", [])[:5]
            synthesis_summary = ""
            if synthesis:
                synthesis_summary = synthesis.get("executive_summary", "")
            memory_store.save_analysis_memory(
                track=self.track,
                competitors=self.competitors,
                key_insights=key_insights,
                synthesis_summary=synthesis_summary,
            )
        except Exception:
            logger.exception("Failed to save memory for analysis %s", self.id)

    async def _emit_events(self, state: dict) -> None:
        progress = state.get("progress_events", [])
        chunks = state.get("chunk_events", [])
        for event in progress[self._emitted_progress:]:
            await self.emit({"event": "progress", "data": event})
            self._record({"role": "agent", "type": "progress", "progress": event})
        self._emitted_progress = len(progress)
        for event in chunks[self._emitted_chunks:]:
            await self.emit({"event": "chunk", "data": event})
        self._emitted_chunks = len(chunks)
        # 发送思考步骤事件
        thinking = state.get("thinking_steps", [])
        for step in thinking[self._emitted_thinking:]:
            await self.emit({"event": "thinking", "data": step})
            self._record({"role": "agent", "type": "thinking", "thinkingStep": step})
        self._emitted_thinking = len(thinking)

    async def _resume_graph(self, confirm_data: dict[str, Any], config: dict) -> dict:
        graph = await get_graph()
        await graph.aupdate_state(config, {"confirmation_result": confirm_data})
        return await graph.ainvoke(None, config)

    async def _finish_invoke(self, result: dict) -> None:
        """处理一次图调用结果：发事件 / 等确认 / 完成。"""
        await self._emit_events(result)
        pending = result.get("pending_confirmation")
        if pending and not result.get("confirmation_result"):
            self._interrupted = True
            self.pending_confirm_type = pending.get("type")
            await self.emit({"event": "confirm", "data": pending})
            self._record({"role": "agent", "type": "confirm", "confirm": pending})
            return
        if result.get("done"):
            await self._complete(result)

    async def resume(self, confirm_data: dict[str, Any]) -> None:
        logger.info("Resuming analysis %s with confirm data", self.id)
        try:
            config = {"configurable": {"thread_id": self.thread_id}}

            # 用户确认期间的等待不计入执行预算：续跑时重置截止时间
            self.deadline = time.monotonic() + compute_analysis_timeout(self.depth, len(self.competitors))

            self._interrupted = False
            self.pending_confirm_type = None
            result = await self._resume_graph(confirm_data, config)
            await self._finish_invoke(result)
        except asyncio.CancelledError:
            # 取消发生在续跑阶段：任务已结束，不会再走 run() 的 CancelledError 兜底，
            # 必须在这里落库，否则 DB 状态永远停留在 running（历史记录一直转圈）
            await self._mark_cancelled()
        except Exception as exc:
            logger.exception("Resume failed for analysis %s", self.id)
            await storage.aupdate_analysis_status(self.id, "error")
            await self.emit({"event": "error", "data": {"message": str(exc), "retryable": True}})

    async def continue_run(self) -> None:
        """进程重启后从检查点续跑（不注入用户输入，仅恢复执行）。"""
        logger.info("Continuing recovered analysis %s from checkpoint", self.id)
        try:
            config = {"configurable": {"thread_id": self.thread_id}}
            self.deadline = time.monotonic() + compute_analysis_timeout(self.depth, len(self.competitors))
            graph = await get_graph()
            result = await graph.ainvoke(None, config)
            await self._finish_invoke(result)
        except asyncio.CancelledError:
            # 同 resume：续跑阶段被取消也必须落库，避免假 running
            await self._mark_cancelled()
        except Exception as exc:
            logger.exception("Continue failed for analysis %s", self.id)
            await storage.aupdate_analysis_status(self.id, "error")
            await self.emit({"event": "error", "data": {"message": str(exc), "retryable": True}})

    def confirm(self, data: dict[str, Any]) -> bool:
        # 仅允许在“等待确认”状态提交确认；resume 期间拒绝新的 confirm（防重入）
        if self.cancelled or not self._interrupted or self._resuming:
            logger.warning("Rejecting confirm for %s (cancelled=%s, not waiting=%s or resuming)", self.id, self.cancelled, not self._interrupted)
            return False
        # 用户已提交确认：立即撤销“等待确认”标记，避免重连时回放刚回答过的过期卡片
        self._interrupted = False
        self.pending_confirm_type = None
        if self.task and not self.task.done():
            self.task.cancel()
        self._resuming = True
        self.task = asyncio.create_task(self._resume_wrapper(data))
        return True

    async def _resume_wrapper(self, data: dict[str, Any]) -> None:
        try:
            await self.resume(data)
        finally:
            self._resuming = False

    async def _mark_cancelled(self) -> None:
        """落库「已取消」并通知订阅者。供各处取消路径复用，保证历史记录不再停留 running。

        已正常完成的会话（_finished）不做任何处理，避免把 completed 覆盖成 cancelled。
        """
        if self._finished:
            return
        try:
            await storage.aupdate_analysis_status(self.id, "cancelled")
        except Exception:
            logger.exception("Failed to persist cancelled status for %s", self.id)
        try:
            await self.emit({"event": "error", "data": {"message": "分析已取消", "retryable": False}})
        except Exception:
            logger.exception("Failed to emit cancel event for %s", self.id)

    def cancel(self) -> None:
        self.cancelled = True
        if self.task and not self.task.done():
            # 任务仍在执行：CancelledError 会在 run()/resume() 的异常分支里落库
            self.task.cancel()
            return
        if self._finished:
            # 已正常完成：不能把 completed 覆盖成 cancelled（否则刚生成的报告状态丢失）
            return
        # 任务已结束（典型场景：正在等待用户确认）：不会再有任何异常分支兜底，
        # 必须同步落库取消状态，否则该记录在 DB 中永远是 running（前端历史一直转圈）
        try:
            storage.update_analysis_status(self.id, "cancelled")
        except Exception:
            logger.exception("Failed to mark cancelled (finished task) session %s", self.id)
        # 通知仍在订阅的 SSE 客户端（如有）；持有引用防止任务被 GC 中途丢弃
        try:
            loop = asyncio.get_running_loop()
            self._notify_task = loop.create_task(
                self.emit({"event": "error", "data": {"message": "分析已取消", "retryable": False}})
            )
        except RuntimeError:
            pass


class AnalysisStore:
    def __init__(self) -> None:
        self.sessions: dict[str, AnalysisSession] = {}

    def create(self, track: str, competitors: list[str], depth: Depth | None, search_provider: str = "tavily",
               owner: str = "", batch_id: str = "", template: dict[str, Any] | None = None) -> AnalysisSession:
        self._cleanup()
        session = AnalysisSession(track=track, competitors=competitors, depth=depth, search_provider=search_provider,
                                  owner=owner, batch_id=batch_id, template=template)
        # 启动即落库（status=running）：进程重启后可从 DB 恢复会话
        try:
            storage.save_analysis(
                analysis_id=session.id,
                track=session.track,
                competitors=session.competitors,
                depth=session.depth or "broad",
                report_md="",
                sources=[],
                created_at=session.created_at_iso,
                conversation=session.conversation,
                status="running",
                client_id=session.owner,
                search_provider=session.search_provider,
                batch_id=session.batch_id,
            )
        except Exception:
            logger.exception("Failed to persist running session %s（进程重启后将无法恢复该会话）", session.id)
        self.sessions[session.id] = session
        session.task = asyncio.create_task(session.run())
        return session

    def get(self, analysis_id: str) -> AnalysisSession:
        """获取会话；内存中不存在时尝试从 DB 恢复 running 会话（重启恢复）。"""
        session = self.sessions.get(analysis_id)
        if session is not None:
            return session
        record = storage.get_analysis(analysis_id)
        if not record or record.get("status") != "running":
            raise KeyError(analysis_id)
        # 恢复会话：从检查点继续执行（AsyncSqliteSaver 已持久化图状态）
        session = AnalysisSession.reconstruct(record)
        # 并发请求可能已恢复同一会话：避免创建重复的 continue_run 任务
        existing = self.sessions.get(analysis_id)
        if existing is not None:
            return existing
        self.sessions[analysis_id] = session
        logger.info("Recovered session %s from DB (track: %s)", analysis_id, session.track)
        session.task = asyncio.create_task(session.continue_run())
        return session

    def drop(self, analysis_id: str) -> None:
        session = self.sessions.pop(analysis_id, None)
        if session is not None:
            session.cancel()

    def _cleanup_interrupted(self) -> None:
        """配合历史清理接口：移除内存中已不在运行态的会话（已完成/取消/中断）。"""
        for sid in list(self.sessions):
            session = self.sessions[sid]
            if not session._finished and not session.cancelled and session.task and not session.task.done():
                continue  # 仍在运行，保留
            session.cancel()
            self.sessions.pop(sid, None)

    def _cleanup(self) -> None:
        if len(self.sessions) <= MAX_SESSIONS:
            return
        # 优先驱逐已完成会话，其次才驱逐运行中的
        def sort_key(item):
            session = item[1]
            return (0 if session._finished or session.cancelled else 1, session.created_at)
        sorted_sessions = sorted(self.sessions.items(), key=sort_key)
        to_remove = len(self.sessions) - MAX_SESSIONS
        for sid, session in sorted_sessions[:to_remove]:
            if not session._finished and not session.cancelled:
                for sub in list(session.subscribers):
                    try:
                        sub["queue"].put_nowait({
                            "event": "error",
                            "data": {"message": "服务器并发会话数已达上限，您的分析已被终止，请稍后重试", "retryable": True},
                        })
                    except Exception:
                        pass
                session.cancel()
                storage.update_analysis_status(sid, "error")
            # 已完成/已取消的会话直接驱逐，不改写 DB 状态（避免已完成报告从历史列表消失）
            self.sessions.pop(sid)
            logger.warning("Evicted session %s due to capacity limit", sid)


store = AnalysisStore()
