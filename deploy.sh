#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# 竞品分析 Agent 一键部署脚本（在服务器上执行）
#
# 使用前提：
#   1. 本地已构建并导出镜像: docker save -o competitive-analysis-agent.tar ...
#   2. 已上传以下文件到本脚本所在目录:
#      - competitive-analysis-agent.tar
#      - .env  (LLM_API_KEY / TAVILY_API_KEY / LLM_BASE_URL / LLM_MODEL)
#
# 执行方式:
#   bash deploy.sh
# ============================================================

APP_NAME="competitive-analysis"
IMAGE_NAME="competitive-analysis-agent:latest"
TAR_FILE="competitive-analysis-agent.tar"
PORT="${PORT:-8000}"
DATA_VOLUME="${DATA_VOLUME:-analysis-data}"
VERSION="${VERSION:-v1.5.9}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

info() { echo -e "\033[32m[INFO]\033[0m $*"; }
warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[31m[ERROR]\033[0m $*"; exit 1; }

# ---------- 1. 检查 docker 环境 ----------
command -v docker >/dev/null 2>&1 || err "未检测到 docker，请先安装: curl -fsSL https://get.docker.com | sh"
docker info >/dev/null 2>&1 || err "docker 未运行或当前用户无权限（试试 sudo 或加入 docker 组）"

# ---------- 2. 加载镜像（镜像已存在则跳过；无通用命名 tar 时探测版本化命名 tar） ----------
if docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    info "镜像 $IMAGE_NAME 已存在，跳过加载"
else
    TAR_FILE_RESOLVED=""
    if [ -f "$TAR_FILE" ]; then
        TAR_FILE_RESOLVED="$TAR_FILE"
    else
        ALT_TAR="$(ls -t "$SCRIPT_DIR"/competitive-analysis-agent-v*.tar 2>/dev/null | head -1 || true)"
        if [ -n "$ALT_TAR" ] && [ -f "$ALT_TAR" ]; then
            TAR_FILE_RESOLVED="$ALT_TAR"
            info "未找到 $TAR_FILE，使用版本化镜像包: $TAR_FILE_RESOLVED"
        fi
    fi
    if [ -n "$TAR_FILE_RESOLVED" ]; then
        info "加载镜像 $TAR_FILE_RESOLVED ..."
        docker load -i "$TAR_FILE_RESOLVED"
    else
        err "找不到镜像包 $TAR_FILE，且本机没有镜像 $IMAGE_NAME；请先上传镜像包到本目录，或执行 docker build 构建镜像"
    fi
fi

# ---------- 3. 检查 .env 配置 ----------
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        warn "已从 .env.example 生成 .env，请先编辑填入真实密钥，再重新运行本脚本"
        exit 1
    else
        err "缺少 .env 配置，请创建 .env 并填入 LLM_API_KEY、TAVILY_API_KEY 等"
    fi
fi
if grep -q "^LLM_API_KEY=[[:space:]]*$" .env || ! grep -q "^LLM_API_KEY=" .env; then
    warn "LLM_API_KEY 可能为空，AI 功能将不可用，请检查 .env"
fi
if grep -q "^TAVILY_API_KEY=[[:space:]]*$" .env || ! grep -q "^TAVILY_API_KEY=" .env; then
    warn "TAVILY_API_KEY 可能为空，联网搜索功能将不可用，请检查 .env"
fi
if grep -q "^ZHIHU_API_KEY=[[:space:]]*$" .env || ! grep -q "^ZHIHU_API_KEY=" .env; then
    warn "ZHIHU_API_KEY 可能为空，知乎搜索（全网搜索/直答）将不可用，请检查 .env"
fi

# ---------- 4. FRONTEND_ORIGIN 自动补齐 ----------
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' | head -n1)"
if ! grep -q "^FRONTEND_ORIGIN=" .env; then
    ORIGIN="http://${SERVER_IP:-localhost}:${PORT}"
    echo "FRONTEND_ORIGIN=${ORIGIN}" >> .env
    info "已在 .env 追加 FRONTEND_ORIGIN=${ORIGIN}（如需域名请手动修改后重启）"
else
    info "FRONTEND_ORIGIN 已在 .env 中配置"
fi

# ---------- 5. 停止并删除旧容器 ----------
if docker ps -a --format '{{.Names}}' | grep -qx "$APP_NAME"; then
    info "停止旧容器 $APP_NAME ..."
    docker rm -f "$APP_NAME" >/dev/null
fi

# ---------- 6. 启动新容器 ----------
info "启动容器 $APP_NAME (端口 ${PORT}) ..."
docker run -d \
    --name "$APP_NAME" \
    --restart unless-stopped \
    -p "${PORT}:8000" \
    --env-file .env \
    -e "APP_VERSION=${VERSION}" \
    -v "${DATA_VOLUME}:/data" \
    "$IMAGE_NAME"

# ---------- 7. 健康检查 ----------
info "等待服务启动 ..."
for i in $(seq 1 30); do
    sleep 1
    if curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
        break
    fi
done

HEALTH="$(curl -sf "http://localhost:${PORT}/api/health" 2>/dev/null || echo '{}')"
echo ""
info "部署完成！"
echo "------------------------------------------------------------"
echo "  访问地址:  http://${SERVER_IP:-<服务器IP>}:${PORT}"
echo "  部署版本:  ${VERSION} (APP_VERSION=${VERSION})"
echo "  健康检查:  $HEALTH"
echo "  查看日志:  docker logs -f $APP_NAME"
echo "  更新部署:  重新上传 tar 后再次执行 bash deploy.sh"
echo "------------------------------------------------------------"
