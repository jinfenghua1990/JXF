#!/bin/bash
# ============================================================
# claims-system 自动同步：跟着 GitHub main 的最新提交走
#
#   1) 查 GitHub API 取 main 最新 commit SHA
#   2) 读运行中容器镜像的 revision 标签（构建时写入）
#   3) 相同 → 退出；不同 → 确认该 SHA 的镜像已由 Actions 发布
#   4) 调用 deploy/update.sh 部署不可变镜像（sha-<SHA>）
#
# 用法：
#   sudo bash deploy/autoupdate.sh            # 正常检查并更新
#   sudo bash deploy/autoupdate.sh --dry-run  # 只报告，不动
# ============================================================
set -uo pipefail
export PATH=/usr/local/bin:/usr/local/sbin:$PATH

APP_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
REPO=jinfenghua1990/JXF
IMAGE_PREFIX=ghcr.io/jinfenghua1990/jxf
LOG=$APP_DIR/backups/autoupdate.log
LOCK=/tmp/claims_autoupdate.lock
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p "$APP_DIR/backups"
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

exec 9>"$LOCK"
flock -n 9 || { log "上一轮还在跑，跳过"; exit 0; }

# ---------- 1. 远端最新 SHA ----------
LATEST=$(curl -fsS -m 20 "https://api.github.com/repos/$REPO/commits/main" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["sha"])' 2>/dev/null)
if [ -z "$LATEST" ]; then
  log "查询 GitHub 失败（网络？），本轮跳过"
  exit 1
fi

# ---------- 2. 当前运行版本 ----------
RUNNING=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' claims-app 2>/dev/null || echo "")
log "运行中 ${RUNNING:-未知} / 远端 ${LATEST:0:12}"

if [ "$RUNNING" = "$LATEST" ]; then
  log "已是最新"
  exit 0
fi

# ---------- 3. 镜像是否已发布 ----------
TARGET="$IMAGE_PREFIX:sha-$LATEST"
if ! docker manifest inspect "$TARGET" >/dev/null 2>&1; then
  log "镜像 $TARGET 还没发布（Actions 可能仍在构建），下轮再试"
  exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  log "[dry-run] 将部署 $TARGET"
  exit 0
fi

# ---------- 4. 部署 ----------
log "发现新版本，开始部署 $TARGET"
if bash "$APP_DIR/deploy/update.sh" "$TARGET" >>"$LOG" 2>&1; then
  log "自动更新成功 ✓ ${LATEST:0:12}"
  exit 0
fi
log "✗ 自动更新失败，详见 $LOG（update.sh 已尝试回滚）"
exit 1
