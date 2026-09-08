#!/usr/bin/env bash
set -euo pipefail
# 后端同步/校验脚本：
#   1. rsync backend-src -> /tmp/opencode/build/app（排除 __pycache__/*.pyc）
#   2. 全量 ast 语法检查
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEST=/tmp/opencode/build/app

echo "[1/2] 同步到镜像构建目录 $DEST ..."
mkdir -p "$DEST"
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' ./ "$DEST/"
echo "      OK"

echo "[2/2] 语法检查全部 .py ..."
FAIL=0
while IFS= read -r f; do
  if ! python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
    echo "      FAIL: $f"
    FAIL=1
  fi
done < <(find . -name "*.py")
if [ "$FAIL" = "0" ]; then
  echo "      OK（$(find . -name '*.py' | wc -l) 个文件）"
else
  echo "      存在语法错误，请修复后重试"
  exit 1
fi
