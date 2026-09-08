import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .agent.graph import compile_graph, set_graph
from .api.analysis import router as analysis_router
from .api.templates import router as templates_router
from .config import get_settings
from .storage import _ensure_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
CHANGELOG_FILE = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def resolve_env(s) -> str:
    """环境判定：复用 config.Settings.effective_env（APP_ENV 显式指定或按版本号后缀推断）。"""
    return s.effective_env


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_db()
    # 启动时清理「假分析中」：进程异常退出/重启后，DB 中残留的 running 记录
    # 统一标记为 interrupted，前端不再显示转圈、且可正常删除。
    try:
        from .storage import amark_all_running_interrupted
        _ = await amark_all_running_interrupted() or None
    except Exception:
        logger.exception("Failed to mark stale running analyses as interrupted")
    logger.info("竞品分析 Agent API starting up")

    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from .storage import DB_PATH

        async with AsyncSqliteSaver.from_conn_string(str(DB_PATH)) as saver:
            set_graph(compile_graph(saver))
            logger.info("LangGraph compiled with AsyncSqliteSaver checkpoint")
            yield
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver

        set_graph(compile_graph(MemorySaver()))
        logger.warning("langgraph-checkpoint-sqlite not installed, using MemorySaver (sessions lost on restart)")
        yield

    # 优雅停机时也把仍在运行的任务标记为已中断，避免下次启动再出现假 running。
    try:
        from .storage import amark_all_running_interrupted
        _ = await amark_all_running_interrupted() or None
    except Exception:
        logger.exception("Failed to mark stale running analyses as interrupted on shutdown")
    logger.info("竞品分析 Agent API shutting down")


app = FastAPI(title="竞品分析 Agent", version=get_settings().app_version, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analysis_router, prefix="/api/analysis", tags=["analysis"])
app.include_router(templates_router, prefix="/api", tags=["templates"])


@app.get("/api/version")
async def version() -> dict[str, str]:
    """版本号接口：生产环境返回 vX.Y.Z，测试/预发返回带 beta/rc 后缀，与 Git 标签保持一致。"""
    s = get_settings()
    return {"version": s.app_version, "name": "竞品分析 Agent", "env": resolve_env(s)}


def _parse_changelog(text: str) -> list[dict]:
    """解析 CHANGELOG.md：## vX.Y.Z - 日期 为版本块，### 小节的 - 项为清单；文件顺序即版本倒序。"""
    versions: list[dict] = []
    cur: dict | None = None
    section: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^##\s+(v[\w.\-]+)\s*-\s*(.*)$", line)
        if m:
            cur = {"version": m.group(1), "date": m.group(2).strip(), "sections": []}
            section = None
            versions.append(cur)
            continue
        if line.startswith("### ") and cur is not None:
            title = line[4:].strip()
            # 版本追溯/维护说明等内部小节不进入用户可见的更新日志
            if title in ("版本追溯",):
                section = None
                continue
            section = {"title": title, "items": []}
            cur["sections"].append(section)
            continue
        if line.startswith("- ") and section is not None:
            section["items"].append(line[2:].strip())
    return versions


def _version_key(version: str) -> tuple[int, ...]:
    """解析 vX.Y.Z[-suffix] 为可比较元组（后缀版本与正式版同序，保证 stable 在前）。"""
    core = re.sub(r"[-+].*$", "", version.lstrip("vV"))
    parts = []
    for seg in core.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


@app.get("/api/changelog")
async def changelog() -> dict:
    """更新日志：按 CHANGELOG.md 解析，按版本号倒序返回（当前版本标记 current）。"""
    s = get_settings()
    versions = _parse_changelog(CHANGELOG_FILE.read_text(encoding="utf-8")) if CHANGELOG_FILE.exists() else []
    # 版本号倒序（v2.6.0-beta.1 与 v2.6.0 同核心版本时 stable 优先）
    versions.sort(key=lambda v: (_version_key(v["version"]), v["version"].startswith(("-",)) is False), reverse=True)
    for v in versions:
        v["current"] = v["version"] == s.app_version or s.app_version.startswith(v["version"] + "-")
    return {"versions": versions}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    checks: dict[str, str] = {}

    from .config import get_settings
    s = get_settings()
    from .tools.web_search import resolve_provider
    # 只返回是否配置，不暴露具体密钥状态
    checks["llm_configured"] = "yes" if s.llm_api_key else "no"
    checks["tavily_configured"] = "yes" if s.tavily_api_key else "no"
    checks["zhihu_configured"] = "yes" if s.zhihu_api_key else "no"
    checks["bocha_configured"] = "yes" if s.bocha_api_key else "no"
    effective = resolve_provider(None)
    checks["search_provider"] = effective or "none"

    from .agent.graph import get_graph
    try:
        await get_graph()
        checks["graph"] = "ok"
    except Exception:
        checks["graph"] = "not_initialized"

    try:
        from .storage import _ensure_db, DB_PATH
        _ensure_db()
        checks["sqlite"] = "ok" if DB_PATH.parent.exists() else "missing_dir"
    except Exception:
        checks["sqlite"] = "error"

    # search_provider 展示实际生效来源（值为 provider 名），不计入 ok 判定
    all_ok = all(v in ("ok", "yes") for k, v in checks.items() if k != "search_provider")
    return {"status": "ok" if all_ok else "degraded", "checks": checks}


if FRONTEND_DIST.exists():
    @app.get("/assets/{file_path:path}")
    async def serve_assets(file_path: str) -> Any:
        """静态资源：no-store 禁用浏览器缓存，前端资源变更后用户刷新即可拿到最新版。"""
        file_path = Path(file_path)
        if not file_path.name or ".." in str(file_path) or file_path.is_absolute():
            return FileResponse(str(FRONTEND_DIST / "index.html"))
        file = FRONTEND_DIST / "assets" / file_path
        if file.exists() and file.is_file():
            return FileResponse(str(file), headers={"Cache-Control": "no-store"})
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str) -> FileResponse:
        file_path = FRONTEND_DIST / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path), headers={"Cache-Control": "no-store"})
        return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-store"})

    logger.info("Frontend dist mounted at / (SPA fallback enabled)")
else:
    logger.warning(
        "Frontend dist not found at %s — run `npm run build` in frontend/ first, "
        "or use dev mode with vite proxy",
        FRONTEND_DIST,
    )