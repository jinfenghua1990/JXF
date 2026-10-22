#!/bin/bash
# ============================================================
# claims-system 生产镜像更新
#   只部署 GitHub Actions 已发布的不可变镜像；不在 NAS 上拉源码或构建。
#
# 用法：
#   sudo bash deploy/update.sh                          # 更新到 .env 里 APP_IMAGE 指向的版本
#   sudo bash deploy/update.sh ghcr.io/jinfenghua1990/jxf:sha-<40位SHA>
#   sudo bash deploy/update.sh ghcr.io/jinfenghua1990/jxf@sha256:<64位摘要>
#
# 流程：备份数据库与 data → 拉镜像（失败重试，不动现有容器）→ 切镜像
#       → 重建 app → 等健康检查 → 失败自动切回上一个镜像
# ============================================================
set -uo pipefail
export PATH=/usr/local/bin:/usr/local/sbin:$PATH

APP_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_BASE=$APP_DIR/docker-compose.yml
COMPOSE_PROD=$APP_DIR/deploy/override-image.yml
ENV_FILE=$APP_DIR/.env
IMAGE_PREFIX=ghcr.io/jinfenghua1990/jxf

log() { echo "[$(date '+%F %T')] $*"; }
die() { log "✗ $*" >&2; exit 1; }

[ -f "$COMPOSE_BASE" ] || die "找不到 $COMPOSE_BASE"
[ -f "$COMPOSE_PROD" ] || die "找不到 $COMPOSE_PROD"
[ -f "$ENV_FILE" ]     || die "找不到 $ENV_FILE"

dc() { docker compose --project-directory "$APP_DIR" -f "$COMPOSE_BASE" -f "$COMPOSE_PROD" "$@"; }

read_env() { sed -n "s/^$1=//p" "$ENV_FILE" | head -n 1 | tr -d '"'"'"; }

# ---------- 目标镜像 ----------
OLD_IMAGE=$(read_env APP_IMAGE)
TARGET=${1:-${OLD_IMAGE:-$IMAGE_PREFIX:latest}}

# 只接受本仓库的镜像：版本标签 / sha 标签 / 不可变摘要，拒绝 latest 之外的浮动引用
case "$TARGET" in
  "$IMAGE_PREFIX":v*|"$IMAGE_PREFIX":sha-*|"$IMAGE_PREFIX":latest) ;;
  "$IMAGE_PREFIX"@sha256:*) ;;
  *) die "拒绝不认识的镜像地址：$TARGET（只允许 $IMAGE_PREFIX 的 v*/sha-*/latest 或 @sha256 摘要）" ;;
esac

set_env_image() {
  local image=$1 tmp="$ENV_FILE.tmp.$$"
  if ! awk -v image="$image" '
    BEGIN { c = 0 }
    /^APP_IMAGE=/ { c++; print "APP_IMAGE=" image; next }
    { print }
    END { if (c > 1) exit 42; if (c == 0) print "APP_IMAGE=" image }
  ' "$ENV_FILE" > "$tmp"; then
    rm -f "$tmp"; return 1
  fi
  chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
}

pull_retry() {
  local max=4 n=1 delay
  while [ "$n" -le "$max" ]; do
    log "拉取镜像（$n/$max）..."
    if dc pull --quiet app; then return 0; fi
    delay=$((n * 10))
    log "拉取失败，${delay}s 后重试；现有容器未受影响"
    sleep "$delay"; n=$((n + 1))
  done
  return 1
}

# ---------- 已经就是目标版本 ----------
CURRENT_ID=$(docker inspect --format '{{.Image}}' claims-app 2>/dev/null || echo "")
if [ -n "$OLD_IMAGE" ] && [ "$OLD_IMAGE" = "$TARGET" ] && [ -n "$CURRENT_ID" ]; then
  log "APP_IMAGE 已是 $TARGET，仍需确认容器是否在跑该镜像"
fi

# ---------- 备份 ----------
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=$APP_DIR/backups/update-$STAMP
mkdir -p "$BACKUP_DIR" || die "无法创建备份目录"

log "[1/6] 备份数据库..."
if ! docker exec claims-db sh -c 'pg_dump -U claims -d claims -Fc --no-owner' > "$BACKUP_DIR/db.dump" 2>"$BACKUP_DIR/db.err"; then
  rm -rf "$BACKUP_DIR"
  die "数据库备份失败，未做任何变更（详见 db.err）"
fi
[ -s "$BACKUP_DIR/db.dump" ] || { rm -rf "$BACKUP_DIR"; die "数据库备份为空，已停止"; }

log "[2/6] 备份 data/..."
if ! tar -C "$APP_DIR/data" -czf "$BACKUP_DIR/data.tar.gz" . 2>/dev/null; then
  log "⚠ data/ 备份失败（不影响继续，但请留意）"
else
  log "    data 备份 $(du -h "$BACKUP_DIR/data.tar.gz" | cut -f1)"
fi

# ---------- 切换镜像 ----------
log "[3/6] 固定目标镜像：$TARGET"
set_env_image "$TARGET" || die "写入 .env 失败"

if ! pull_retry; then
  [ -n "$OLD_IMAGE" ] && set_env_image "$OLD_IMAGE"
  die "镜像拉取失败，已恢复原配置，现有容器未切换"
fi

log "[4/6] 重建 app 容器..."
if ! dc up -d --no-build --no-deps app; then
  log "✗ 容器启动失败，回滚"
  if [ -n "$OLD_IMAGE" ]; then set_env_image "$OLD_IMAGE"; dc up -d --no-build --no-deps app || true; fi
  die "重建失败，已尝试回滚"
fi

# ---------- 健康检查 ----------
log "[5/6] 等待健康检查（最多 90s）..."
healthy=0
for i in $(seq 1 30); do
  st=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' claims-app 2>/dev/null || echo none)
  if [ "$st" = "healthy" ]; then healthy=1; break; fi
  if [ "$st" = "unhealthy" ]; then break; fi
  sleep 3
done

if [ "$healthy" != 1 ]; then
  log "✗ 健康检查未通过（状态 $st），回滚到 ${OLD_IMAGE:-无}"
  docker logs --tail 40 claims-app 2>&1 | sed 's/^/    /'
  if [ -n "$OLD_IMAGE" ]; then
    set_env_image "$OLD_IMAGE"
    if dc pull --quiet app >/dev/null 2>&1 && dc up -d --no-build --no-deps app; then
      log "已回滚到 $OLD_IMAGE"
    else
      log "✗ 回滚也失败了，需要人工介入"
    fi
  fi
  exit 1
fi

log "[6/6] 更新完成 ✓"
docker inspect --format '    镜像: {{.Config.Image}}' claims-app
docker inspect --format '    版本: {{index .Config.Labels "org.opencontainers.image.revision"}}' claims-app
log "备份保留在 $BACKUP_DIR"
