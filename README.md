# 竞品分析 Agent v1.5.10 - 源码包（源码仓库）

本仓库为完整源码（含前端构建产物），可自行构建 Docker 镜像并部署，适合二次开发与源码审计。

本项目提供两种部署方式：

| 方式 | 仓库 | 适用场景 |
|------|------|----------|
| **方式一：镜像包直接部署** | [Competitor-Analysis-Agent](https://github.com/avazhangjing/Competitor-Analysis-Agent) | 只想快速上线，不需要改代码。加载现成镜像即可运行 |
| **方式二：源码构建部署**（本仓库） | `Competitor-Analysis-Agent-src` | 需要二次开发、审计源码。需自行 `docker build`（3-10 分钟） |

两个仓库内容一致（同一版本发布），按需二选一即可。

## 一、拉取本仓库

本仓库无 LFS 大文件，普通 git clone 即可：

```bash
git clone https://github.com/avazhangjing/Competitor-Analysis-Agent-src.git
cd Competitor-Analysis-Agent-src
```

## 二、方式一：镜像包直接部署（不想构建选这个）

```bash
# 1. 安装 git-lfs（镜像包约 176MB 走 LFS，必须先安装，否则只会拉到 134 字节指针文件）
apt-get install -y git-lfs        # Debian/Ubuntu；CentOS: yum install -y git-lfs
git lfs install

# 2. 拉取镜像仓库（镜像包 + deploy.sh + .env.example）
git clone https://github.com/avazhangjing/Competitor-Analysis-Agent.git
cd Competitor-Analysis-Agent

# 3. 校验 tar 约 176MB（若只有 134 字节执行 git lfs pull），然后一键部署
ls -lh competitive-analysis-agent-v1.5.10.tar
cp .env.example .env && vi .env    # 必填: LLM_API_KEY, TAVILY_API_KEY
bash deploy.sh
```

## 三、方式二：源码构建部署（本仓库）

### 1. 构建镜像

```bash
# 在本仓库目录下执行（构建过程会下载依赖，需联网，耗时约 3-10 分钟）
docker build -t competitive-analysis-agent:latest .

# 可选：导出镜像包，便于拷贝到其他机器
docker save -o competitive-analysis-agent-v1.5.10.tar competitive-analysis-agent:latest
```

### 2. 配置环境变量

```bash
cp .env.example .env
vi .env
```

必填项：

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | LLM API Key（OpenAI 兼容，默认 deepseek） |
| `TAVILY_API_KEY` | Tavily 搜索 Key（https://app.tavily.com 免费注册） |

可选：`ZHIHU_API_KEY`（知乎搜索）、`BOCHA_API_KEY`（博查搜索）、`LLM_BASE_URL`、`LLM_MODEL`、`SEARCH_PROVIDER`、`APP_VERSION`、`APP_ENV`。

> `.env` 放在哪个目录都行：后端会自动向上查找 `/app/.env` 或父目录 `.env`。

### 3. 启动

#### 方式 A：一键部署（推荐）

```bash
# .env 和 deploy.sh 在同一目录，直接执行
bash deploy.sh
# 自动：检测镜像（已构建则跳过加载）→ 检查 .env → 启动容器 → 健康检查
```

#### 方式 B：手动 docker run

```bash
docker run -d \
    --name competitive-analysis \
    --restart unless-stopped \
    -p 8000:8000 \
    --env-file .env \
    -v analysis-data:/data \
    competitive-analysis-agent:latest
```

#### 方式 C：本地直接跑（无 Docker，需 Python 3.11）

```bash
pip install -r backend/requirements.txt
cp .env.example .env   # 填入密钥
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 四、验证与访问

- 访问地址：`http://<服务器IP>:8000`
- 健康检查：`curl http://localhost:8000/api/health`
- 版本接口：`curl http://localhost:8000/api/version` → `{"version":"v1.5.10",...}`
- 查看日志：`docker logs -f competitive-analysis`

## 五、常见问题

| 问题 | 解决 |
|------|------|
| 页面能开但接口报错 | 检查 CORS：用域名访问需在 .env 设置 `FRONTEND_ORIGIN=http://你的域名`，改后重启容器 |
| AI 功能不可用 | 检查 `LLM_API_KEY` 是否正确填写 |
| 搜索不可用 | 检查 `TAVILY_API_KEY`；服务端回退策略由 `SEARCH_PROVIDER` 控制（默认 auto：bocha→tavily→zhihu） |
| 数据在哪 | 容器内 `/data`（SQLite），挂载 volume `analysis-data` 后数据持久化，更新版本不丢数据 |
| 端口冲突 | 改 `-p 8000:8000` 左侧端口，或 `PORT=8080 bash deploy.sh` |
| deploy.sh 提示找不到镜像包 | 本机已有镜像会自动跳过加载；都没有时请先 `docker build` 或使用方式一（镜像仓库） |

## 六、包内容

```
Competitor-Analysis-Agent-src/
├── Dockerfile           # 镜像构建文件（Python 3.11）
├── app/                 # 后端源码（FastAPI + LangGraph）
│   ├── main.py          # 入口：/api/version /api/changelog /api/health、静态资源
│   ├── config.py        # 环境变量配置（读取 .env）
│   ├── storage.py       # SQLite 持久化（/data/analysis.db）
│   ├── api/             # 分析 API + 模版 API
│   ├── agent/           # LangGraph 图（planner/search/reflect/analyze/synthesize/report）
│   ├── tools/           # web_search / bocha_search / zhihu_search / web_fetch / mcp_client
│   └── ...
├── backend/
│   └── requirements.txt # 后端 Python 依赖清单
├── frontend/dist/       # 前端构建产物（SPA，index.html + assets）
├── deploy.sh            # 一键部署脚本
├── .env.example         # 环境变量配置模板
└── CHANGELOG.md         # 变更日志（/api/changelog 接口读取）
```

## 七、版本信息

- 版本号：v1.5.10（2026-09-09）
- 本版主要变更：修复安全检查「假 ok」——`.env` 中 API Key 为模板占位符（如 `your_zhihu_access_secret`）时仍判定为已配置的问题；占位符现视为未配置（`/api/health` 显示 degraded、前端来源置灰、`/start` 返回 400）
- 变更记录：见 CHANGELOG.md
