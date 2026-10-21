#!/bin/sh
# 本地 git 包装：NAS 未装 git，用 alpine/git 容器操作本仓库
# 用法: sudo sh scripts/git.sh <git 子命令>   例: sudo sh scripts/git.sh status
exec sudo /usr/local/bin/docker run --rm \
  -v /volume1/docker/claims-system:/w \
  -w /w \
  --entrypoint /usr/bin/git \
  alpine/git \
  -c user.name=jinfe \
  -c user.email=jinfenghua1990@users.noreply.github.com \
  -c safe.directory=/w \
  "$@"
