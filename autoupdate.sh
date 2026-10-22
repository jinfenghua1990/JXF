#!/bin/bash
# ============================================================
# claims-system 自动更新：从 GitHub 拉最新代码并部署
#   1) fetch origin/main，比对本地 HEAD
#   2) 无变化 → 退出（绝大多数情况）
#   3) 只有 .md 文档变更 → 仅同步，不重启服务
#   4) 有代码变更 → 备份 → reset → deploy.sh → 失败自动回滚
#
# 用法：
#   sudo bash autoupdate.sh            # 正常自动更新
#   sudo bash autoupdate.sh --dry-run  # 只看会做什么，不动代码
#   sudo bash autoupdate.sh --force    # 强制重新部署当前 origin/main
# ============================================================
set -uo pipefail
export PATH=/usr/local/bin:/usr/local/sbin:$PATH

APP_DIR=/volume1/docker/claims-system
REPO_URL="https://github.com/jinfenghua1990/JXF.git"
BRANCH=main
LOG=$APP_DIR/backups/autoupdate.log
LOCK=/tmp/claims_autoupdate.lock

DRY_RUN=0
FORCE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
  esac
done

mkdir -p "$APP_DIR/backups"
# 日志超过 1MB 就只保留最近 500 行
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

cd "$APP_DIR" || { echo "无法进入 $APP_DIR"; exit 1; }

# 防并发：上一轮还没跑完就跳过
exec 9>"$LOCK"
if ! flock -n 9; then
  log "已有实例在运行，跳过本轮"
  exit 0
fi

gitx() { sudo -n sh scripts/git.sh "$@" 2>&1; }

# ---------- 1. 远程 ----------
if ! gitx remote get-url origin >/dev/null 2>&1; then
  log "未配置 origin，写入 $REPO_URL"
  [ "$DRY_RUN" = 1 ] || gitx remote add origin "$REPO_URL"
fi

if ! gitx fetch origin "$BRANCH" --quiet; then
  log "拉取失败（网络或 GitHub 不可达），本轮跳过"
  exit 1
fi

LOCAL=$(gitx rev-parse HEAD | tr -d '\r')
REMOTE=$(gitx rev-parse "origin/$BRANCH" | tr -d '\r')
log "本地 ${LOCAL:0:8} / 远程 ${REMOTE:0:8}"

if [ "$LOCAL" = "$REMOTE" ] && [ "$FORCE" = 0 ]; then
  log "已是最新，无需更新"
  exit 0
fi

# 本地有未提交改动时不动，避免覆盖线上手工修改
if [ -n "$(gitx status --porcelain | grep -vE '^\?\? ' )" ]; then
  log "工作区有未提交改动，为安全起见跳过自动更新（请先提交或还原）"
  exit 2
fi

CHANGED=$(gitx diff --name-only "$LOCAL" "$REMOTE")
if [ -z "$CHANGED" ] && [ "$FORCE" = 0 ]; then
  log "无文件差异，退出"
  exit 0
fi

CODE_CHANGED=$(printf '%s\n' "$CHANGED" | grep -vE '\.md$' | grep -v '^$' | head -20)
log "变更文件："
printf '%s\n' "$CHANGED" | sed 's/^/    /' | tee -a "$LOG"

if [ -z "$CODE_CHANGED" ] && [ "$FORCE" = 0 ]; then
  log "只有文档变更，仅同步代码，不重启服务"
  [ "$DRY_RUN" = 1 ] || gitx reset --hard "origin/$BRANCH" >/dev/null
  exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  log "[dry-run] 将执行：reset --hard $REMOTE → deploy.sh"
  exit 0
fi

# ---------- 2. 更新 + 部署 ----------
TS=$(date +%Y%m%d-%H%M%S)
log "开始更新到 ${REMOTE:0:8}"
if ! gitx reset --hard "origin/$BRANCH" >/dev/null; then
  log "reset 失败，终止"
  exit 1
fi

log "执行 deploy.sh ..."
if sudo -n bash deploy.sh >>"$LOG" 2>&1; then
  log "部署成功 ✓ 当前版本 $(cat VERSION 2>/dev/null) / ${REMOTE:0:8}"
  exit 0
fi

# ---------- 3. 失败回滚 ----------
log "部署失败，回滚到 ${LOCAL:0:8}"
if gitx reset --hard "$LOCAL" >/dev/null; then
  if sudo -n bash deploy.sh >>"$LOG" 2>&1; then
    log "已回滚到 ${LOCAL:0:8}，服务恢复正常"
  else
    log "回滚后仍部署失败！需人工介入"
  fi
else
  log "回滚 reset 失败！需人工介入"
fi
exit 1
