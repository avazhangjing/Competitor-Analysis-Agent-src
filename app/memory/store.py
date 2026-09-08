"""长期记忆存储。

使用 SQLite 持久化存储分析经验，支持：
- 赛道知识沉淀（关键洞察、竞品列表）
- 用户偏好记录（常分析赛道、深度偏好）
- 按赛道/关键词检索历史记忆
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[3]
MEMORY_DB_PATH = ROOT_DIR / "data" / "memory.db"

_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL,
    track TEXT NOT NULL,
    content TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_track ON memories(track);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);

CREATE TABLE IF NOT EXISTS user_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pref_key TEXT NOT NULL UNIQUE,
    pref_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_lock = threading.Lock()
_initialized = False


class MemoryStore:
    """长期记忆存储器。"""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or MEMORY_DB_PATH
        self._ensure_db()

    def _ensure_db(self) -> None:
        global _initialized
        if _initialized:
            return
        with _lock:
            if _initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.executescript(_MEMORY_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            _initialized = True
            logger.info("Memory database initialized at %s", self.db_path)

    def save_analysis_memory(
        self,
        track: str,
        competitors: list[str],
        key_insights: list[str],
        synthesis_summary: str = "",
        evaluation_score: float = 0.0,
    ) -> None:
        """分析完成后，将关键发现沉淀为长期记忆。"""
        # 1. 存储赛道知识（竞品列表 + 核心洞察）
        knowledge_content = json.dumps({
            "competitors": competitors,
            "key_insights": key_insights[:10],  # 最多保留10条洞察
            "synthesis_summary": synthesis_summary[:500],
            "evaluation_score": evaluation_score,
        }, ensure_ascii=False)

        keywords = " ".join(competitors) + " " + track

        self._insert_memory(
            memory_type="track_knowledge",
            track=track,
            content=knowledge_content,
            keywords=keywords,
            metadata={"competitor_count": len(competitors)},
        )

        # 2. 更新用户偏好（最近分析的赛道）
        self._update_preference("last_track", track)
        self._update_preference("last_competitors", json.dumps(competitors, ensure_ascii=False))

        logger.info("Saved analysis memory for track '%s' with %d competitors", track, len(competitors))

    def save_user_preference(self, key: str, value: str) -> None:
        """记录用户偏好。"""
        self._update_preference(key, value)

    def get_relevant_memories(self, track: str, limit: int = 5) -> list[dict[str, Any]]:
        """检索与指定赛道相关的历史记忆。"""
        with _lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                # 精确匹配赛道
                rows = conn.execute(
                    "SELECT * FROM memories WHERE track = ? ORDER BY created_at DESC LIMIT ?",
                    (track, limit),
                ).fetchall()

                # 如果精确匹配不足，尝试关键词模糊匹配
                if len(rows) < limit:
                    track_keywords = track.split()
                    for kw in track_keywords:
                        if len(kw) < 2:
                            continue
                        # 转义 LIKE 通配符，避免用户输入中的 %/_ 扩大匹配范围（L9）
                        escaped = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        extra_rows = conn.execute(
                            "SELECT * FROM memories WHERE keywords LIKE ? ESCAPE '\\' AND track != ? "
                            "ORDER BY created_at DESC LIMIT ?",
                            (f"%{escaped}%", track, limit - len(rows)),
                        ).fetchall()
                        rows.extend(extra_rows)
                        if len(rows) >= limit:
                            break
            finally:
                conn.close()

        return [self._row_to_memory(row) for row in rows[:limit]]

    def get_user_preferences(self) -> dict[str, str]:
        """获取所有用户偏好。"""
        with _lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT pref_key, pref_value FROM user_preferences").fetchall()
            finally:
                conn.close()
        return {row["pref_key"]: row["pref_value"] for row in rows}

    def get_memory_stats(self) -> dict[str, Any]:
        """获取记忆系统统计信息。"""
        with _lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                by_type = conn.execute(
                    "SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type"
                ).fetchall()
                tracks = conn.execute(
                    "SELECT DISTINCT track FROM memories"
                ).fetchall()
            finally:
                conn.close()
        return {
            "total_memories": total,
            "by_type": {row[0]: row[1] for row in by_type},
            "unique_tracks": [row[0] for row in tracks],
        }

    def _insert_memory(
        self,
        memory_type: str,
        track: str,
        content: str,
        keywords: str = "",
        metadata: dict | None = None,
    ) -> None:
        with _lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "INSERT INTO memories (memory_type, track, content, keywords, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        memory_type,
                        track,
                        content,
                        keywords,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def _update_preference(self, key: str, value: str) -> None:
        with _lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO user_preferences (pref_key, pref_value, updated_at) "
                    "VALUES (?, ?, ?)",
                    (key, value, datetime.now().isoformat(timespec="seconds")),
                )
                conn.commit()
            finally:
                conn.close()

    def _row_to_memory(self, row: sqlite3.Row) -> dict[str, Any]:
        content_raw = row["content"]
        try:
            content = json.loads(content_raw)
        except (json.JSONDecodeError, TypeError):
            content = {"raw": content_raw}

        metadata_raw = row["metadata"] if "metadata" in row.keys() else "{}"
        try:
            metadata = json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            metadata = {}

        return {
            "id": row["id"],
            "memory_type": row["memory_type"],
            "track": row["track"],
            "content": content,
            "keywords": row["keywords"],
            "metadata": metadata,
            "created_at": row["created_at"],
        }


# 全局单例
_memory_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """获取全局记忆存储实例。"""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store
