#!/bin/bash
# ============================================================
# 一键部署：备份 → 主机级 JS 校验 → 构建(含镜像内门禁) → 重启 → 冒烟
#
# 用法：sudo bash deploy.sh          （任意目录下都可执行，按脚本自身位置定位）
#
# 任一步失败都会中止，且此时旧容器仍在服务；镜像内还有一层 JS 门禁，
# 所以「前端语法错误」不会把线上打挂。
# ============================================================
set -euo pipefail
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin

APP_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

# root 下直接用 docker，普通用户走 sudo
if [ "$(id -u)" = "0" ]; then
  dc() { docker compose "$@"; }
else
  dc() { sudo docker compose "$@"; }
fi

# 端口从 .env 读，读不到用 8021
PORT=$(grep -E '^APP_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '\r' || true)
PORT=${PORT:-8021}
BASE="http://127.0.0.1:${PORT}"

TS=$(date +%Y%m%d-%H%M%S)
echo "[1/5] 备份当前版本 -> backups/deploy-$TS/"
mkdir -p "backups/deploy-$TS"
cp -p backend/app/static/*.html backend/app/api.py "backups/deploy-$TS/" 2>/dev/null || true

echo "[2/5] 主机级 JS 语法校验"
node tools/check_inline_js.js backend/app/static

echo "[3/5] 构建（镜像内还有一层门禁，任一失败构建中止、旧容器继续服务）"
dc build

echo "[4/5] 重启容器"
dc up -d --build

echo "[5/5] 冒烟（最多等 60 秒）"
OK=0
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null "${BASE}/healthz"; then OK=1; break; fi
  sleep 2
done
[ "$OK" = "1" ] && echo "  healthz OK" || { echo "  healthz FAIL（60 秒内未就绪）"; dc logs --tail=40 app; exit 1; }
curl -sf -o /dev/null "${BASE}/" && echo "  首页 OK" || { echo "  首页 FAIL"; exit 1; }

set -a; . ./.env; set +a
TOKEN=$(curl -s -X POST "${BASE}/api/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)
[ -n "$TOKEN" ] && echo "  管理员登录 OK" || { echo "  管理员登录 FAIL"; exit 1; }
curl -sf -H "Authorization: Bearer $TOKEN" "${BASE}/api/page-version" > /dev/null && echo "  page-version OK" || { echo "  page-version FAIL"; exit 1; }
curl -sf -H "Authorization: Bearer $TOKEN" "${BASE}/api/me" > /dev/null && echo "  me OK" || { echo "  me FAIL"; exit 1; }
curl -sf -H "Authorization: Bearer $TOKEN" "${BASE}/api/todo?limit=1" > /dev/null && echo "  待办接口 OK" || { echo "  待办接口 FAIL"; exit 1; }
echo "部署完成 ✓（备份保留在 backups/deploy-$TS/）"
