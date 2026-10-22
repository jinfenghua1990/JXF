#!/bin/bash
# ============================================================
# claims-system 自动同步：跟着 GitHub main 的最新提交走
#
# 为什么不用镜像模式：本机到 ghcr.io 的 Blob 下载实测只有 23KB/s，
# 一个 66MB 的镜像要拉一小时以上且中途会断。而 NAS 到 GitHub 的
# git 协议是通的（ls-remote 2.5 秒），所以走「拉源码 + 本地构建」。
#
#   1) git fetch origin main，比较本地 HEAD 与 origin/main
#   2) 相同 → 退出，什么都不做
#   3) 不同 → 备份当前源码 → reset --hard 到新版
#   4) 跑 deploy.sh（JS 门禁 → 构建 → 重启 → 5 项冒烟）
#   5) 失败 → 回到旧 commit 重新构建，服务不留半死状态
#
# 用法：
#   sudo bash deploy/autoupdate.sh            # 检查并更新
#   sudo bash deploy/autoupdate.sh --dry-run  # 只报告，不动
#
# crontab（DSM 的 /etc/crontab）：
#   */30 * * * * root bash /volume1/docker/claims-system/deploy/autoupdate.sh >/dev/null 2>&1
# ============================================================
set -uo pipefail
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin

# 本脚本会在运行途中被 git reset --hard 换成新版本。bash 是按字节偏移边读边执行
# 脚本的，文件在脚下被替换会导致后面的语句错乱（曾出现 prune 被跳过）。
# 所以先把自己拷到 /tmp，再从副本执行，运行期间文件内容不再变化。
if [ -z "${CLAIMS_AUTOUPDATE_CHILD:-}" ]; then
  _APP_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
  _SELF_COPY="/tmp/claims_autoupdate.$$.sh"
  cp -- "$0" "$_SELF_COPY" || exit 1
  export CLAIMS_AUTOUPDATE_CHILD=1 APP_DIR="$_APP_DIR"
  exec bash "$_SELF_COPY" "$@"
fi
# 只删自己那份副本，绝不碰仓库里的原文件
case "$0" in
  /tmp/claims_autoupdate.*.sh) trap 'rm -f "$0"' EXIT ;;
esac

APP_DIR=${APP_DIR:?APP_DIR 未传递}
BRANCH=${BRANCH:-main}
LOG=$APP_DIR/backups/autoupdate.log
LOCK=/tmp/claims_autoupdate.lock
DOCKER=${DOCKER:-/usr/local/bin/docker}
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p "$APP_DIR/backups"

# 日志轮转：超过 2MB 只保留最后 2000 行
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 2097152 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# 只保留最近 20 份备份，避免每轮更新都把磁盘堆满
prune_backups() {
  local pat d
  for pat in "autoupdate-*" "deploy-*"; do
    ls -1dt "$APP_DIR"/backups/$pat 2>/dev/null | tail -n +21 | while IFS= read -r d; do
      rm -rf "$d"
    done
  done
}

# 防重入：上一轮构建还没跑完就跳过
exec 9>"$LOCK"
flock -n 9 || { log "上一轮还在跑，跳过"; exit 0; }

# NAS 没装 git，借 alpine/git 容器操作仓库（镜像已在本地缓存）
git_() {
  "$DOCKER" run --rm -v "$APP_DIR:/w" -w /w --entrypoint /usr/bin/git alpine/git \
    -c safe.directory=/w -c user.name=jinfe \
    -c user.email=jinfenghua1990@users.noreply.github.com "$@"
}

cd "$APP_DIR" || exit 1

# ---------- 1. 取远端最新 ----------
if ! git_ fetch --prune origin "$BRANCH" >>"$LOG" 2>&1; then
  log "git fetch 失败（网络？），本轮跳过，服务不受影响"
  exit 0
fi

LOCAL=$(git_ rev-parse HEAD 2>/dev/null)
REMOTE=$(git_ rev-parse "origin/$BRANCH" 2>/dev/null)
if [ -z "$LOCAL" ] || [ -z "$REMOTE" ]; then
  log "读取 commit 失败（LOCAL=$LOCAL REMOTE=$REMOTE），本轮跳过"
  exit 1
fi

if [ "$LOCAL" = "$REMOTE" ]; then
  log "已是最新 ${LOCAL:0:7}"
  exit 0
fi

log "发现新版本 ${LOCAL:0:7} → ${REMOTE:0:7}"

if [ "$DRY_RUN" = 1 ]; then
  git_ log --oneline "$LOCAL..$REMOTE" 2>&1 | tee -a "$LOG" | head -20
  log "[dry-run] 未执行更新"
  exit 0
fi

# ---------- 2. 备份当前源码（回滚锚点） ----------
TS=$(date +%Y%m%d-%H%M%S)
BK="$APP_DIR/backups/autoupdate-$TS"
mkdir -p "$BK"
cp -p backend/app/api.py "$BK"/ 2>/dev/null
cp -p backend/app/static/*.html "$BK"/ 2>/dev/null
printf '%s\n' "$LOCAL" > "$BK/ROLLBACK_SHA"
git_ log --oneline "$LOCAL..$REMOTE" > "$BK/CHANGES" 2>&1

# ---------- 3. 切到新版本 ----------
if ! git_ reset --hard "origin/$BRANCH" >>"$LOG" 2>&1; then
  # 未跟踪文件挡住了 reset 才会走到这里；git clean 不动 .gitignore 里的 .env/data/backups
  log "reset 失败，清理未跟踪文件后重试"
  git_ clean -fd >>"$LOG" 2>&1
  if ! git_ reset --hard "origin/$BRANCH" >>"$LOG" 2>&1; then
    log "✗ 切换代码失败，放弃本轮（当前代码未变）"
    exit 1
  fi
fi

# ---------- 4. 构建 + 重启 + 冒烟 ----------
if bash "$APP_DIR/deploy.sh" >>"$LOG" 2>&1; then
  prune_backups
  log "✓ 更新成功，当前版本 ${REMOTE:0:7}（备份：$BK）"
  exit 0
fi

# ---------- 5. 失败回滚 ----------
log "✗ 部署失败，回滚到 ${LOCAL:0:7}"
if ! git_ reset --hard "$LOCAL" >>"$LOG" 2>&1; then
  log "✗✗ 回滚切代码失败，需人工介入！备份在 $BK"
  exit 1
fi
if bash "$APP_DIR/deploy.sh" >>"$LOG" 2>&1; then
  log "已回滚到 ${LOCAL:0:7} 并恢复服务"
else
  log "✗✗ 回滚后仍部署失败，需人工介入！备份在 $BK"
fi
exit 1
