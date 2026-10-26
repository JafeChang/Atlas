#!/usr/bin/env bash
# cron 的入口；ATLAS_LIVE 必须由操作者在 cron 中显式开启。
set -euo pipefail

atlas_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$atlas_root"
mkdir -p .tmp_atlas
exec 9>.tmp_atlas/collection.lock
if ! flock -n 9; then
    printf '%s 上一轮采集仍在运行，本轮跳过。\n' "$(date --iso-8601=seconds)"
    exit 0
fi

# 采集不触发行业分类；已启用的译题/简介任务独立处理新增文章。
export ATLAS_COGNITION=0
.venv-new/bin/python -u -m atlas.compose run --due-only --allow-partial "$@"
if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet atlas-briefs.timer; then
    if ! systemctl --no-ask-password --no-block start atlas-briefs.service; then
        printf '采集已完成，但增量译题/简介任务未能立即启动；定时器会继续补齐。\n' >&2
    fi
fi
