#!/usr/bin/env bash
# T-205 修订：搜索测试门禁（把退出码与摘要固定成可引用的一行）。
set -u
cd /mnt/c/Users/bestz/Documents/projects/Atlas || exit 90
PY=./.venv-new/bin/python
if [ "$#" -eq 0 ]; then
  set -- tests/test_search_cjk.py tests/test_search_index.py \
         tests/test_search_invariants.py tests/test_search_query.py \
         tests/test_search_realdata.py
fi
"$PY" -m pytest "$@" -q -p no:cacheprovider -p no:randomly 2>&1 | tail -n 40
status=${PIPESTATUS[0]}
echo "PYTEST_EXIT=${status}"
exit "$status"
