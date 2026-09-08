# 后端维护说明

## 背景

后端源码（FastAPI + LangGraph）此前**只存在于镜像与构建目录**（`/tmp/opencode/build/app`），不在 git 仓库中，修改无法追溯。从 2026-08-12 起镜像到本目录，纳入版本管理。

## 目录结构

```
backend-src/
├── main.py          # FastAPI 入口：/api/version /api/changelog /api/health、静态资源服务（no-store）
├── config.py        # Settings（env_file 读取 /app/.env 或上级目录 .env）
├── storage.py       # SQLite 持久化（/data/analysis.db，WAL + busy_timeout；analyses + templates 表）
├── templates.py     # 分析模版：默认模版（只读）+ 自定义模版（维度/关键词，SQLite 持久化）
├── api/analysis.py  # 分析 API：start/stream/confirm/cancel/history/trace
├── api/templates.py # 模版 API：GET/POST/PUT/DELETE /api/templates（默认模版不可改删）
├── agent/           # LangGraph 图与各 Agent 节点（planner/search/reflect/analyze/synthesize/report）
│   ├── runner.py    # AnalysisSession/AnalysisStore（SSE 订阅、回放、驱逐、confirm 守卫）
│   ├── graph.py     # 图编排（progress_events/thinking_steps 下发）
│   ├── llm.py       # LLM 调用（重试/超时/token 累加器 contextvars 隔离）
│   └── prompts/     # 各 Agent 的 prompt 模板
├── tools/           # web_search / bocha_search / zhihu_search / web_fetch（SSRF 逐跳校验）/ mcp_client
├── memory/          # 长期记忆（SQLite，LIKE 通配符已转义）
├── evaluation/      # 评估（benchmark/evaluator）
├── tracing.py       # 执行链路追踪
└── events.py        # SSE 事件格式
```

## 维护约定

1. **backend-src 是后端源码的唯一权威**，修改必须在本目录进行；
2. 修改后运行 `bash apply.sh` 同步到镜像构建目录 `/tmp/opencode/build/app`（排除 `__pycache__`），并做全量语法检查（ast.parse 全部 .py）；
3. 发布流程：`bash apply.sh` → 重新 `docker build`（`/tmp/opencode/build`）→ `docker save` → `bash deploy.sh`；
4. 与 frontend-src 一致：不得在本目录放置密钥（`.env` 不入库）、数据库文件、日志。

## 关键设计点（改前必读）

- **共享记录模式**：历史不做 client_id 隔离，`_require_owner`/`_client_id` 为历史残留（仅 start 写入 owner 兼容旧数据）；
- **confirm 守卫**：`confirm_analysis` 要求 `session._interrupted` 且 `pending_confirm_type` 匹配，否则 409；`runner.confirm()` 内部 `_resuming` 防重入；
- **SSE 回放**：subscribe() 只回放最新 confirm/progress/thinking（修复确认后界面从头开始）；
- **SQLite 并发**：storage 与 LangGraph 检查点共用 `/data/analysis.db`，`_connect()` 强制 WAL/busy_timeout=10000/synchronous=NORMAL；storage 的 async 调用一律走 `asave_analysis`/`alist_analyses` 等 to_thread 包装，禁止在 async 函数里直接调同步 sqlite3；
- **多选搜索引擎分屏**：`/start` 支持 `search_providers`（数组），按每个来源 `store.create(...)` 各建一个会话（各自 asyncio 任务并行），响应含 `sessions:[{analysis_id, search_provider}]` 与 `batch_id`；`search_provider` 单值仍兼容；`storage` 已持久化 `search_provider`/`batch_id` 列（含迁移，旧库自动补列）；
- **搜索来源已移除「自动」**：`ALLOWED_PROVIDERS = (tavily, bocha, zhihu, zhihu_site, zhida)`，缺省 `DEFAULT_PROVIDER="tavily"`；前端不再提供「自动」选项。服务端 `SEARCH_PROVIDER=auto` 仍可用作内部回退（`resolve_provider` 自动挑配置的 bocha→tavily→zhihu）；
- **广度/深度合并**：图流程不再下发 `confirm_depth`，`depth` 默认 `"deep"`（统一含反思补采），`depth` 字段仅供历史/旧调用兼容；
- **分析模版**：`/start` 新增 `template_id`（引用 `/api/templates` 里保存的默认/自定义模版）与 `template`（内联自定义模版 `{name, dimensions:[{name, keywords}]}`）；`plan_research` 在提供模版时按模版维度+关键词直接构建研究计划（确定性强，不走 LLM 重排）。SQLite `templates` 表持久化自定义模版，`/api/templates` 提供 CRUD；
- **batch 历史分组**：多来源一次任务多个子会话共享 `batch_id`，`/history/list` 按 `batch_id` 聚合成一条记录（status 取子会话运行态、含 `searchProviders`/`analysisIds`）；`/history/{batch_id}` 详情返回聚合 + `children`（各来源完整报告，供弹窗切换）；`DELETE /history/{batch_id}` 整批删除并终止 running 子会话；单来源 `batch_id` 为其自身 id，行为与旧版一致；
- **token 统计**：`llm.py` 累加器为 `ContextVar`（按 asyncio.Task 隔离），节点内 `reset_token_tracker()` 后 `get_accumulated_tokens()`；
- **SSRF**：`web_fetch` 关闭自动重定向，逐跳 `_is_url_safe` + `_check_ip_safe`（最多 5 跳）；
- **静态资源**：`/assets` 与 SPA 兜底均返回 `Cache-Control: no-store`；前端资源文件名带版本后缀（如 `.v121.js`）强制刷新。
