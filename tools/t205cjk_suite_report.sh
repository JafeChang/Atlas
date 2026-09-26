#!/usr/bin/env bash
# T-205 修订：把"收集到的用例数 / 失败是否与搜索域有关"落成可引用的一行。
set -u
cd /mnt/c/Users/bestz/Documents/projects/Atlas || exit 90
PY=./.venv-new/bin/python
"$PY" -m pytest tests -p no:cacheprovider --co -q > /tmp/t205cjk_collected.txt 2>&1
awk -F': ' '/: [0-9]+$/ {sum += $2} END {printf "COLLECTED=%d\n", sum}' /tmp/t205cjk_collected.txt
"$PY" -m pytest tests -q -p no:cacheprovider > /tmp/t205cjk_full.txt 2>&1
echo "FULL_SUITE_EXIT=$?"
awk '/^FAILED/ {print "FAILED_LINE=" $0}' /tmp/t205cjk_full.txt
awk '/^FAILED/ && /search/ {n++} END {printf "SEARCH_RELATED_FAILURES=%d\n", n + 0}' /tmp/t205cjk_full.txt
