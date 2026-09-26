#!/usr/bin/env bash
# T-205 修订的**门禁范围**（本次提交涉及的代码）精确计数：搜索域五个测试文件。
set -u
REPO=/mnt/c/Users/bestz/Documents/projects/Atlas
PY="$REPO/.venv-new/bin/python"
XML=/tmp/t205cjk_junit_search.xml
cd "$REPO" || exit 90
"$PY" -m pytest \
  tests/test_search_cjk.py tests/test_search_index.py tests/test_search_invariants.py \
  tests/test_search_query.py tests/test_search_realdata.py \
  -q -p no:cacheprovider --junit-xml="$XML" > /tmp/t205cjk_junit_search_stdout.txt 2>&1
echo "PYTEST_EXIT=$?"
"$PY" - "$XML" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suite = root.find("testsuite") if root.tag == "testsuites" else root
print("collected:", suite.get("tests"))
print("failures :", suite.get("failures"))
print("errors   :", suite.get("errors"))
print("skipped  :", suite.get("skipped"))
print("time     :", suite.get("time"))
PY
