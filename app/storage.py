import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
DB_PATH = ROOT_DIR / "data" / "analysis.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    track TEXT NOT NULL,
    competitors TEXT NOT NULL,
    depth TEXT NOT NULL,
    report_md TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    research_plan TEXT NOT NULL DEFAULT '{}',
    synthesis TEXT,
    evaluation TEXT,
    conversation TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'completed',
    client_id TEXT NOT NULL DEFAULT '',
    search_provider TEXT NOT NULL DEFAULT 'auto',
    batch_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_track ON analyses(track);
CREATE INDEX IF NOT EXISTS idx_analyses_client ON analyses(client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    dimensions TEXT NOT NULL DEFAULT '[]',
    is_custom INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""

_lock = threading.Lock()
_initialized = False
_migrated = False


def _connect() -> sqlite3.Connection:
    """建立连接并应用并发优化：WAL + busy_timeout，避免与 LangGraph 检查点争锁。

    storage 与 AsyncSqliteSaver 共用同一 DB 文件；WAL 允许读写并发，
    busy_timeout 让写锁竞争时等待而非立刻抛 database is locked。
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        logger.warning("Failed to apply SQLite PRAGMAs (ignored)", exc_info=True)
    return conn


def _ensure_db() -> None:
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized = True
        logger.info("SQLite database initialized at %s", DB_PATH)


def _maybe_migrate() -> None:
    """Add research_plan, synthesis and evaluation columns if missing (for existing DBs). Runs once."""
    global _migrated
    if _migrated:
        return
    with _lock:
        if _migrated:
            return
        conn = _connect()
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
            for col, ddl in (
                ("research_plan", "ALTER TABLE analyses ADD COLUMN research_plan TEXT NOT NULL DEFAULT '{}'"),
                ("synthesis", "ALTER TABLE analyses ADD COLUMN synthesis TEXT"),
                ("evaluation", "ALTER TABLE analyses ADD COLUMN evaluation TEXT"),
                ("conversation", "ALTER TABLE analyses ADD COLUMN conversation TEXT NOT NULL DEFAULT '[]'"),
                ("status", "ALTER TABLE analyses ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"),
                ("client_id", "ALTER TABLE analyses ADD COLUMN client_id TEXT NOT NULL DEFAULT ''"),
                ("search_provider", "ALTER TABLE analyses ADD COLUMN search_provider TEXT NOT NULL DEFAULT 'auto'"),
                ("batch_id", "ALTER TABLE analyses ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''"),
            ):
                if col in cols:
                    continue
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    # 多进程/多 worker 并发迁移时后到者会报 duplicate column，幂等容忍
                    if "duplicate column" in str(exc).lower():
                        logger.warning("Column %s already exists (concurrent migration), skipping", col)
                    else:
                        raise
            conn.commit()
        finally:
            conn.close()
        _migrated = True


def save_analysis(
    analysis_id: str,
    track: str,
    competitors: list[str],
    depth: str,
    report_md: str,
    sources: list[dict[str, str]],
    created_at: str,
    research_plan: dict | None = None,
    synthesis: dict | None = None,
    evaluation: dict | None = None,
    conversation: list | None = None,
    status: str = "completed",
    client_id: str = "",
    search_provider: str = "auto",
    batch_id: str = "",
) -> None:
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO analyses (id, track, competitors, depth, report_md, sources, research_plan, synthesis, evaluation, conversation, status, client_id, search_provider, batch_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "track=excluded.track, competitors=excluded.competitors, depth=excluded.depth, "
                "report_md=excluded.report_md, sources=excluded.sources, research_plan=excluded.research_plan, "
                "synthesis=excluded.synthesis, evaluation=excluded.evaluation, conversation=excluded.conversation, "
                "status=excluded.status, client_id=excluded.client_id, search_provider=excluded.search_provider, "
                "batch_id=excluded.batch_id, created_at=excluded.created_at",
                (
                    analysis_id,
                    track,
                    json.dumps(competitors, ensure_ascii=False),
                    depth,
                    report_md,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(research_plan or {}, ensure_ascii=False),
                    json.dumps(synthesis, ensure_ascii=False) if synthesis else None,
                    json.dumps(evaluation, ensure_ascii=False) if evaluation else None,
                    json.dumps(conversation or [], ensure_ascii=False),
                    status,
                    client_id,
                    search_provider,
                    batch_id,
                    created_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def update_analysis_status(analysis_id: str, status: str) -> None:
    """更新分析状态（running/completed/error/cancelled）。"""
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE analyses SET status = ? WHERE id = ?", (status, analysis_id))
            conn.commit()
        finally:
            conn.close()


def list_analyses(limit: int = 30, client_id: str | None = None) -> list[dict[str, Any]]:
    """列出历史记录（任意状态，含 interrupted/error/cancelled，便于统一管理/删除）。

    修复：不再隐藏 error/cancelled，任何状态的记录都应可见、可删除。
    """
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            if client_id:
                rows = conn.execute(
                    "SELECT id, track, competitors, depth, report_md, sources, research_plan, synthesis, evaluation, status, client_id, search_provider, batch_id, created_at "
                    "FROM analyses WHERE client_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (client_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, track, competitors, depth, report_md, sources, research_plan, synthesis, evaluation, status, client_id, search_provider, batch_id, created_at "
                    "FROM analyses ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(row) for row in rows]


def mark_all_running_interrupted() -> int:
    """启动时把残留的 running 记录批量标记为 interrupted。

    进程异常退出/重启时，内存中的任务已消亡，DB 中 status='running' 的记录无法
    再由该进程回写状态，属于「假分析中」脏数据。启动时统一标记为已中断。
    """
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute("UPDATE analyses SET status = 'interrupted' WHERE status = 'running'")
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


def list_analyses_by_batch(batch_id: str, limit: int = 100) -> list[dict[str, Any]]:
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, track, competitors, depth, report_md, sources, research_plan, synthesis, evaluation, conversation, status, client_id, search_provider, batch_id, created_at "
                "FROM analyses WHERE batch_id = ? ORDER BY created_at ASC LIMIT ?",
                (batch_id, limit),
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(row) for row in rows]


def delete_analyses_by_batch(batch_id: str) -> int:
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute("DELETE FROM analyses WHERE batch_id = ?", (batch_id,))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


def get_analysis(analysis_id: str) -> dict[str, Any] | None:
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, track, competitors, depth, report_md, sources, research_plan, synthesis, evaluation, conversation, status, client_id, search_provider, batch_id, created_at "
                "FROM analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row) if row else None


def delete_analysis(analysis_id: str) -> bool:
    _ensure_db()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
        finally:
            conn.close()
    return deleted


def delete_interrupted() -> tuple[int, int]:
    """一键清理全部「已中断」记录（含其所属 batch 子会话）。

    规则：
    - 单记录：status='interrupted' 且非 batch（batch_id 空或等于自身 id）→ 直接删除；
    - batch：某 batch 下所有子会话都已是 interrupted → 整批删除；只要还有 running/其他状态则保留。
    返回 (删除的记录数, 删除的 batch 组数)。
    """
    _ensure_db()
    _maybe_migrate()
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, batch_id FROM analyses WHERE status = 'interrupted'"
            ).fetchall()
            singles: list[str] = []
            batch_ids: set[str] = set()
            for r in rows:
                bid = r["batch_id"] or ""
                if not bid or bid == r["id"]:
                    singles.append(r["id"])
                else:
                    batch_ids.add(bid)
            deleted = 0
            batches = 0
            for bid in batch_ids:
                statuses = [x[0] for x in conn.execute(
                    "SELECT status FROM analyses WHERE batch_id = ?", (bid,)
                ).fetchall()]
                if statuses and all(s == "interrupted" for s in statuses):
                    cur = conn.execute("DELETE FROM analyses WHERE batch_id = ?", (bid,))
                    deleted += cur.rowcount
                    batches += 1
            for sid in singles:
                cur = conn.execute("DELETE FROM analyses WHERE id = ?", (sid,))
                deleted += cur.rowcount
            conn.commit()
            return (deleted, batches)
        finally:
            conn.close()


def _safe_json_loads(raw: Any, default: Any) -> Any:
    """JSON 解析容错：单条损坏数据不影响整个历史列表/详情接口。"""
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted JSON in analyses row (raw=%r...)", str(raw)[:80])
        return default


async def asave_analysis(*args: Any, **kwargs: Any) -> None:
    await asyncio.to_thread(save_analysis, *args, **kwargs)


async def aupdate_analysis_status(*args: Any, **kwargs: Any) -> None:
    await asyncio.to_thread(update_analysis_status, *args, **kwargs)


async def amark_all_running_interrupted(*args: Any, **kwargs: Any) -> int:
    return await asyncio.to_thread(mark_all_running_interrupted, *args, **kwargs)


async def alist_analyses(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_analyses, *args, **kwargs)


async def aget_analysis(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    return await asyncio.to_thread(get_analysis, *args, **kwargs)


async def adelete_analysis(*args: Any, **kwargs: Any) -> bool:
    return await asyncio.to_thread(delete_analysis, *args, **kwargs)


async def alist_analyses_by_batch(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_analyses_by_batch, *args, **kwargs)


async def adelete_analyses_by_batch(*args: Any, **kwargs: Any) -> int:
    return await asyncio.to_thread(delete_analyses_by_batch, *args, **kwargs)


async def adelete_interrupted(*args: Any, **kwargs: Any) -> tuple[int, int]:
    return await asyncio.to_thread(delete_interrupted, *args, **kwargs)


# ---------------------------------------------------------------------------
# 自定义分析模版（custom analysis templates）
# ---------------------------------------------------------------------------
def list_templates() -> list[dict[str, Any]]:
    """列出全部自定义模版（按创建时间倒序）。默认模版由 templates 模块拼装。"""
    _ensure_db()
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, name, dimensions, is_custom, created_at FROM templates "
                "ORDER BY created_at DESC"
            ).fetchall()
        finally:
            conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append({
            "id": row["id"],
            "name": row["name"],
            "dimensions": _safe_json_loads(row["dimensions"], []),
            "is_custom": bool(row["is_custom"]),
            "createdAt": row["created_at"],
        })
    return result


def get_template(template_id: str) -> dict[str, Any] | None:
    _ensure_db()
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, name, dimensions, is_custom, created_at FROM templates WHERE id = ?",
                (template_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "dimensions": _safe_json_loads(row["dimensions"], []),
        "is_custom": bool(row["is_custom"]),
        "createdAt": row["created_at"],
    }


def create_template(template_id: str, name: str, dimensions: list[dict[str, Any]],
                    created_at: str, is_custom: bool = True) -> dict[str, Any]:
    _ensure_db()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO templates (id, name, dimensions, is_custom, created_at) VALUES (?, ?, ?, ?, ?)",
                (template_id, name, json.dumps(dimensions, ensure_ascii=False), int(is_custom), created_at),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "id": template_id,
        "name": name,
        "dimensions": dimensions,
        "is_custom": is_custom,
        "createdAt": created_at,
    }


def update_template(template_id: str, name: str, dimensions: list[dict[str, Any]]) -> bool:
    _ensure_db()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE templates SET name = ?, dimensions = ? WHERE id = ?",
                (name, json.dumps(dimensions, ensure_ascii=False), template_id),
            )
            conn.commit()
            updated = cursor.rowcount > 0
        finally:
            conn.close()
    return updated


def delete_template(template_id: str) -> bool:
    _ensure_db()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
        finally:
            conn.close()
    return deleted


async def alist_templates(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_templates, *args, **kwargs)


async def aget_template(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    return await asyncio.to_thread(get_template, *args, **kwargs)


async def acreate_template(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(create_template, *args, **kwargs)


async def aupdate_template(*args: Any, **kwargs: Any) -> bool:
    return await asyncio.to_thread(update_template, *args, **kwargs)


async def adelete_template(*args: Any, **kwargs: Any) -> bool:
    return await asyncio.to_thread(delete_template, *args, **kwargs)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    competitors = _safe_json_loads(row["competitors"] if row["competitors"] else None, [])
    sources = _safe_json_loads(row["sources"] if row["sources"] else None, [])
    research_plan = _safe_json_loads(row["research_plan"] if "research_plan" in row.keys() else "{}", {})
    synthesis = _safe_json_loads(row["synthesis"] if "synthesis" in row.keys() else None, None)
    evaluation = _safe_json_loads(row["evaluation"] if "evaluation" in row.keys() else None, None)
    conversation = _safe_json_loads(row["conversation"] if "conversation" in row.keys() else None, [])
    status = row["status"] if "status" in row.keys() else "completed"
    client_id = row["client_id"] if "client_id" in row.keys() else ""
    search_provider = row["search_provider"] if "search_provider" in row.keys() else "auto"
    batch_id = row["batch_id"] if "batch_id" in row.keys() else ""
    return {
        "id": row["id"],
        "track": row["track"],
        "competitors": competitors,
        "depth": row["depth"],
        "reportMd": row["report_md"],
        "sources": sources,
        "researchPlan": research_plan,
        "synthesis": synthesis,
        "evaluation": evaluation,
        "conversation": conversation,
        "status": status,
        "clientId": client_id,
        "searchProvider": search_provider,
        "batchId": batch_id,
        "createdAt": row["created_at"],
    }
