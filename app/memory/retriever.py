"""记忆检索器。

实现 RAG 式的长期记忆检索：
- 根据当前分析赛道，检索历史相关记忆
- 将检索到的记忆格式化为可注入 prompt 的上下文
- 支持 BM25 风格的关键词匹配排序

面试话术：
"长期记忆采用 RAG 式按需检索，而不是把全部记忆塞进上下文。
新分析启动时，系统根据赛道关键词检索历史分析经验，
将相关洞察作为先验知识注入 research_plan 的 prompt 中。"
"""

import logging
from typing import Any

from .store import get_memory_store

logger = logging.getLogger(__name__)

# 最多注入的记忆条数，避免上下文过长
MAX_MEMORY_CONTEXT_ITEMS = 3
MAX_MEMORY_CONTEXT_CHARS = 1500


def retrieve_relevant_memories(track: str, competitors: list[str] | None = None) -> str:
    """检索与当前分析相关的历史记忆，格式化为 prompt 上下文。

    Args:
        track: 当前分析的赛道
        competitors: 当前指定的竞品列表（可选）

    Returns:
        格式化的记忆上下文字符串，可直接注入 prompt。
        如果没有相关记忆，返回空字符串。
    """
    store = get_memory_store()
    memories = store.get_relevant_memories(track, limit=MAX_MEMORY_CONTEXT_ITEMS)

    if not memories:
        return ""

    context_parts = []
    total_chars = 0

    for mem in memories:
        formatted = _format_memory_for_prompt(mem, competitors)
        if not formatted:
            continue
        if total_chars + len(formatted) > MAX_MEMORY_CONTEXT_CHARS:
            break
        context_parts.append(formatted)
        total_chars += len(formatted)

    if not context_parts:
        return ""

    header = "【历史分析经验（长期记忆）】\n以下是之前分析同赛道或相关赛道时积累的经验，可作为参考：\n"
    return header + "\n".join(context_parts)


def retrieve_user_preferences_context() -> str:
    """获取用户偏好上下文。"""
    store = get_memory_store()
    prefs = store.get_user_preferences()

    if not prefs:
        return ""

    parts = []
    if prefs.get("last_track"):
        parts.append(f"- 上次分析的赛道：{prefs['last_track']}")
    if prefs.get("preferred_depth"):
        parts.append(f"- 偏好分析深度：{prefs['preferred_depth']}")

    if not parts:
        return ""

    return "【用户偏好】\n" + "\n".join(parts)


def _format_memory_for_prompt(memory: dict[str, Any], current_competitors: list[str] | None) -> str:
    """将单条记忆格式化为 prompt 友好的文本。"""
    mem_type = memory.get("memory_type", "")
    track = memory.get("track", "")
    content = memory.get("content", {})
    created_at = memory.get("created_at", "")

    if mem_type == "track_knowledge":
        return _format_track_knowledge(content, track, created_at, current_competitors)

    return ""


def _format_track_knowledge(
    content: dict,
    track: str,
    created_at: str,
    current_competitors: list[str] | None,
) -> str:
    """格式化赛道知识记忆。"""
    parts = [f"[{created_at}] 赛道「{track}」的历史分析："]

    # 竞品列表
    competitors = content.get("competitors", [])
    if competitors:
        parts.append(f"  已分析竞品：{', '.join(competitors[:8])}")

    # 如果有当前竞品列表，标注哪些是新增的
    if current_competitors and competitors:
        old_set = {c.lower() for c in competitors}
        new_comps = [c for c in current_competitors if c.lower() not in old_set]
        if new_comps:
            parts.append(f"  本次新增竞品：{', '.join(new_comps[:5])}")

    # 核心洞察（最多3条）
    insights = content.get("key_insights", [])
    if insights:
        parts.append("  核心发现：")
        for insight in insights[:3]:
            parts.append(f"    - {insight[:100]}")

    # 综合摘要
    summary = content.get("synthesis_summary", "")
    if summary:
        parts.append(f"  摘要：{summary[:200]}")

    return "\n".join(parts)
