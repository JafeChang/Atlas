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

# 定时更新原文不调用付费模型；需要分类时由操作者单独显式执行。
export ATLAS_COGNITION=0
exec .venv-new/bin/python -u -m atlas.compose run --due-only --allow-partial "$@"
