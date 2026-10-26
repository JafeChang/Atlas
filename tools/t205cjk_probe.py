"""T-205 修订（CJK 逐字切分）用的**只读**真实语料探测脚本。

只读 `data/raw/<频道>/*.json`，统计：
- JSON 文件数、有正文的文件数；
- 含 CJK 码点的文档数（段级与字符级）；
- 每个频道的分布；
- 若干含 CJK 的样本（供人工核对）。

用法（WSL）：
    ./.venv-new/bin/python tools/t205cjk_probe.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_RAW = REPO_ROOT / "data" / "raw"

CJK_RANGES = (
    ("U+4E00-U+9FFF", 0x4E00, 0x9FFF),
    ("U+3400-U+4DBF", 0x3400, 0x4DBF),
    ("U+F900-U+FAFF", 0xF900, 0xFAFF),
    ("U+20000-U+2A6DF", 0x20000, 0x2A6DF),
)


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for _, lo, hi in CJK_RANGES)


def main() -> int:
    if not LEGACY_RAW.is_dir():
        print(f"data/raw 不存在：{LEGACY_RAW}", file=sys.stderr)
        return 2

    json_files = sorted(LEGACY_RAW.rglob("*.json"))
    print(f"JSON 文件总数: {len(json_files)}")

    total_with_body = 0
    total_with_cjk = 0
    cjk_chars = 0
    per_channel: dict[str, list[int]] = {}
    samples: list[tuple[str, str, int]] = []

    for path in json_files:
        try:
            payload = json.loads(path.read_bytes().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        content = payload.get("raw_content")
        if not isinstance(content, str) or not content.strip():
            continue
        total_with_body += 1
        channel = path.parent.name
        count = sum(1 for ch in content if is_cjk(ch))
        bucket = per_channel.setdefault(channel, [0, 0, 0])
        bucket[0] += 1
        if count:
            total_with_cjk += 1
            cjk_chars += count
            bucket[1] += 1
            bucket[2] += count
            if len(samples) < 8:
                first = next(i for i, ch in enumerate(content) if is_cjk(ch))
                samples.append((channel, content[max(0, first - 30) : first + 60], count))

    print(f"有正文的文件数: {total_with_body}")
    print(f"含 CJK 的文件数: {total_with_cjk}")
    print(f"CJK 码点总数: {cjk_chars}")
    print("\n按频道：")
    for channel in sorted(per_channel):
        files, with_cjk, chars = per_channel[channel]
        print(f"  {channel:<22} 有正文 {files:>4}  含 CJK {with_cjk:>4}  CJK 码点 {chars:>8}")

    if samples:
        print("\n样本（含 CJK 的正文片段，前 8 条）：")
        for channel, text, count in samples:
            print(f"  [{channel}] cjk={count} :: {text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
