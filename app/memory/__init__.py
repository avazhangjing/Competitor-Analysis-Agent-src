"""Agent 长期记忆模块。

实现跨会话的记忆持久化和检索：
- 赛道知识沉淀：每次分析完成后存储关键发现
- 用户偏好记忆：记录用户常分析的赛道和偏好
- 分析经验复用：新分析时检索同赛道历史分析作为先验知识

面试话术：
- 短期记忆：LangGraph State + Checkpointer（thread_id 区分会话）
- 长期记忆：SQLite + BM25 关键词检索（RAG 式按需检索）
- 记忆沉淀：分析完成后自动将关键洞察写入长期记忆
"""

from .store import MemoryStore, get_memory_store
from .retriever import retrieve_relevant_memories

__all__ = ["MemoryStore", "get_memory_store", "retrieve_relevant_memories"]
