"""统一 Tracing 模块 - 全链路可观测性。

提供结构化的执行链路追踪：
- 每个节点记录：agent_name, step, input_tokens, output_tokens, latency_ms, status
- 支持导出为 JSON（方便面试展示）
- Token 成本追踪：按节点粒度记录，计算各阶段占比

面试话术：
"系统实现了全链路可观测性，每个 Agent 节点的执行时间、Token 消耗、
状态都会被结构化记录。通过 /trace 接口可以查看完整执行链路，
便于定位性能瓶颈和优化 Token 成本。"
"""

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TraceSpan:
    """单个执行 span（对应一个 Agent 节点的一次执行）。"""

    agent_name: str
    step: str
    start_time: float = 0.0
    end_time: float = 0.0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    status: str = "running"  # running | success | error
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def finish(self, status: str = "success") -> None:
        self.end_time = time.perf_counter()
        self.latency_ms = round((self.end_time - self.start_time) * 1000)
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceRecord:
    """一次完整分析的 Trace 记录。"""

    analysis_id: str
    track: str
    start_time: float = field(default_factory=time.perf_counter)
    end_time: float = 0.0
    total_latency_ms: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    spans: list[TraceSpan] = field(default_factory=list)
    status: str = "running"

    def add_span(self, span: TraceSpan) -> None:
        self.spans.append(span)
        self.total_input_tokens += span.input_tokens
        self.total_output_tokens += span.output_tokens
        self.total_tokens += span.total_tokens

    def finish(self, status: str = "success") -> None:
        self.end_time = time.perf_counter()
        self.total_latency_ms = round((self.end_time - self.start_time) * 1000)
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "track": self.track,
            "total_latency_ms": self.total_latency_ms,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "status": self.status,
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
            "cost_estimate": self._estimate_cost(),
            "token_distribution": self._token_distribution(),
        }

    def _estimate_cost(self) -> dict[str, float]:
        """估算 Token 成本（基于 DeepSeek 定价）。"""
        # DeepSeek-V4-Flash: 输入 1元/百万token，输出 2元/百万token（平峰）
        input_cost = self.total_input_tokens / 1_000_000 * 1.0
        output_cost = self.total_output_tokens / 1_000_000 * 2.0
        return {
            "input_cost_yuan": round(input_cost, 4),
            "output_cost_yuan": round(output_cost, 4),
            "total_cost_yuan": round(input_cost + output_cost, 4),
        }

    def _token_distribution(self) -> dict[str, int]:
        """各 Agent 的 Token 消耗分布。"""
        dist: dict[str, int] = {}
        for span in self.spans:
            key = span.agent_name
            dist[key] = dist.get(key, 0) + span.total_tokens
        return dist


# 全局 Trace 存储（内存中，按 analysis_id 索引）
_trace_store: dict[str, TraceRecord] = {}
_MAX_TRACES = 100


def create_trace(analysis_id: str, track: str) -> TraceRecord:
    """创建新的 Trace 记录。"""
    global _trace_store
    # 清理过多的旧 trace
    if len(_trace_store) > _MAX_TRACES:
        oldest_keys = list(_trace_store.keys())[:len(_trace_store) - _MAX_TRACES]
        for k in oldest_keys:
            del _trace_store[k]

    trace = TraceRecord(analysis_id=analysis_id, track=track)
    _trace_store[analysis_id] = trace
    return trace


def get_trace(analysis_id: str) -> TraceRecord | None:
    """获取指定分析的 Trace 记录。"""
    return _trace_store.get(analysis_id)


def start_span(trace: TraceRecord, agent_name: str, step: str, **metadata) -> TraceSpan:
    """在 Trace 中开始一个新的 span。"""
    span = TraceSpan(
        agent_name=agent_name,
        step=step,
        start_time=time.perf_counter(),
        metadata=metadata,
    )
    return span


def end_span(trace: TraceRecord, span: TraceSpan, status: str = "success",
             input_tokens: int = 0, output_tokens: int = 0, detail: str = "") -> None:
    """结束一个 span 并记录到 trace。"""
    span.finish(status)
    span.input_tokens = input_tokens
    span.output_tokens = output_tokens
    span.total_tokens = input_tokens + output_tokens
    span.detail = detail
    trace.add_span(span)


def build_trace_from_thinking_steps(
    analysis_id: str,
    track: str,
    thinking_steps: list[dict[str, Any]],
    total_elapsed_seconds: float,
) -> TraceRecord:
    """从已有的 thinking_steps 构建 Trace 记录（兼容现有数据）。"""
    trace = TraceRecord(analysis_id=analysis_id, track=track)
    trace.total_latency_ms = round(total_elapsed_seconds * 1000)

    for step_data in thinking_steps:
        span = TraceSpan(
            agent_name=step_data.get("agent", "unknown"),
            step=step_data.get("step", ""),
            latency_ms=step_data.get("elapsed_ms", 0),
            total_tokens=step_data.get("tokens", 0),
            status="success",
            detail=step_data.get("detail", ""),
        )
        trace.add_span(span)

    trace.finish("success")
    _trace_store[analysis_id] = trace
    return trace
