#!/usr/bin/env bash
# 一键部署。NAS / Linux / WSL 均可直接跑。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=============================================="
echo " 三方理赔追责系统 · 部署"
echo "=============================================="

# 1. 检查 Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "[错误] 未找到 docker 命令。请先安装 Docker。"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "[错误] 未找到 docker compose（需要 Compose V2）。"
  exit 1
fi
echo "[1/4] Docker 环境就绪：$(docker --version)"

# 2. 生成 .env
if [ ! -f .env ]; then
  cp .env.example .env
  GEN_PWD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
  if command -v sed >/dev/null 2>&1; then
    sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${GEN_PWD}/" .env
  fi
  echo "[2/4] 已生成 .env，自动设置数据库密码"
else
  echo "[2/4] .env 已存在，保持原配置"
fi

mkdir -p data/uploads data/backups

# 3. 构建并启动
echo "[3/4] 构建镜像并启动服务（首次约需数分钟）..."
docker compose up -d --build

# 4. 健康检查
echo "[4/4] 等待服务就绪..."
PORT=$(grep -E '^APP_PORT=' .env | cut -d= -f2 | tr -d '\r')
PORT=${PORT:-8021}
OK=0
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    OK=1
    break
  fi
  sleep 2
done

echo "=============================================="
if [ "$OK" = "1" ]; then
  echo " 部署成功"
  echo ""
  echo "   访问地址：http://<本机IP>:${PORT}"
  echo "   本机访问：http://127.0.0.1:${PORT}"
  echo ""
  echo "   查看日志：docker compose logs -f app"
  echo "   停止服务：docker compose stop"
  echo "   重新启动：docker compose start"
else
  echo " 服务未在预期时间内就绪"
  echo ""
  echo " 排查命令：docker compose ps"
  echo "          docker compose logs --tail=50 app"
  echo ""
  echo " 常见原因："
  echo "   1) 端口被占用 —— 改 .env 里的 APP_PORT"
  echo "   2) 拉镜像失败 —— .env 里已配国内镜像，若仍失败可换官方源"
  exit 1
fi
echo "=============================================="