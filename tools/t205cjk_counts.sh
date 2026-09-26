#!/usr/bin/env bash
# 用 JUnit XML 统计精确的 passed/failed/skipped/errors 数（本机 pytest 的摘要行不打印）。
set -u
REPO=/mnt/c/Users/bestz/Documents/projects/Atlas
WT=/tmp/atlas-verify-t205cjk
PY="$REPO/.venv-new/bin/python"
XML=/tmp/t205cjk_junit.xml

if [ "${1:-}" = "worktree" ]; then
  cd "$WT" || exit 91
  export PYTHONPATH="$WT/src"
else
  cd "$REPO" || exit 90
fi
"$PY" -m pytest tests -q -p no:cacheprovider --junit-xml="$XML" > /tmp/t205cjk_junit_stdout.txt 2>&1
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
