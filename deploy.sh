#!/bin/bash
# 一键部署：备份 → 主机级 JS 校验 → 构建(含镜像内门禁) → 重启 → 冒烟
# 用法: sudo bash /volume1/docker/claims-system/deploy.sh
set -e
export PATH=/usr/local/bin:/usr/local/sbin:$PATH
cd /volume1/docker/claims-system

TS=$(date +%Y%m%d-%H%M%S)
echo "[1/5] 备份当前版本 -> backups/deploy-$TS/"
mkdir -p backups/deploy-$TS
cp -p backend/app/static/*.html backend/app/api.py backups/deploy-$TS/ 2>/dev/null || true

echo "[2/5] 主机级 JS 语法校验"
node tools/check_inline_js.js backend/app/static

echo "[3/5] 构建（镜像内还有一层门禁，任一失败构建中止、旧容器继续服务）"
sudo docker compose build

echo "[4/5] 重启容器"
sudo docker compose up -d --build

echo "[5/5] 冒烟"
sleep 6
curl -sf -o /dev/null http://127.0.0.1:8021/ && echo "  首页 OK" || { echo "  首页 FAIL"; exit 1; }
curl -sf http://127.0.0.1:8021/healthz > /dev/null && echo "  healthz OK" || { echo "  healthz FAIL"; exit 1; }
curl -sf -H "Authorization: Bearer dev" http://127.0.0.1:8021/api/page-version > /dev/null && echo "  page-version OK" || { echo "  page-version FAIL"; exit 1; }
echo "部署完成 ✓（备份保留在 backups/deploy-$TS/）"
