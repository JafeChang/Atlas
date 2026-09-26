#!/usr/bin/env bash
# 硬规则 4：提交后用**独立 worktree** 复核该提交本身（路径带任务号，避免并行抢占）。
set -u
REPO=/mnt/c/Users/bestz/Documents/projects/Atlas
WT=/tmp/atlas-verify-t205cjk
COMMIT="$1"
PY="$REPO/.venv-new/bin/python"

rm -rf "$WT"
git -C "$REPO" worktree add --detach "$WT" "$COMMIT" || exit 90
echo "=== worktree HEAD ==="
git -C "$WT" log --oneline -1
echo "=== files present ==="
ls "$WT/src/atlas/search"
echo "=== pytest（PYTHONPATH 指向该检出） ==="
cd "$WT" || exit 91
PYTHONPATH="$WT/src" "$PY" -m pytest tests -q -p no:cacheprovider -rs > /tmp/t205cjk_worktree.txt 2>&1
status=$?
tail -n 25 /tmp/t205cjk_worktree.txt
echo "WORKTREE_PYTEST_EXIT=${status}"
exit "$status"
