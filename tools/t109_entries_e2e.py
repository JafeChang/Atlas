"""T-109 补完的**真实数据流证据**：一条命令起前端 → 看见真实标题 → 点一下完成条目级打标。

为什么放在 `tools/` 而不是 pytest
================================

本脚本会**真的往真实库 `data/store/atlas.db` 写一条人工标签**（Confirmed 是 append-only，
删不掉）。pytest 不得往真实库写人工标签，因此它单独放在这里，**由人显式运行**：

```bash
PYTHONPATH=src ./.venv-new/bin/python tools/t109_entries_e2e.py            # 完整闭环
PYTHONPATH=src ./.venv-new/bin/python tools/t109_entries_e2e.py --dry-run  # 只测 CLI 与 GET，不写标签
```

它做四件事，每件都打印可核对的证据：

1. **CLI 真的能起服务**：以子进程跑 `python -m atlas.webapp --port <空闲端口>`，
   打印它的实际启动输出与 URL（判据："用户照着一条命令就能打开界面"）；
2. `GET /feed` 拿到 **200**，并从 HTML 里抓出**真实条目标题**（不是 raw_id）；
3. `POST /label` 打一个**条目级**标签（anchor = 该条目的字符区间）；
4. 用**独立打开的 `sqlite3`**（不经过任何前端代码）证明 `confirmed_labels` 里那一行的
   `anchor_char_start/anchor_char_end` 非空、且落在该条目的区间内；再 `GET /feed` 证明状态回显。

留下的那条标签用 actor=`e2e-t109`、`label_key`/`label_value` 由 `--label-key/--label-value`
指定（默认 `industry` / 该条目文档的行业修正值 —— 一个**客观**维度，不是"有效性"这种主观判断）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_STORE_ROOT = REPO_ROOT / "data" / "store"
DEFAULT_ACTOR = "e2e-t109"


# ---------------------------------------------------------------------- #
# 小工具
# ---------------------------------------------------------------------- #
def free_port() -> int:
    """向内核要一个当前空闲的端口（立刻释放，仅用于挑端口）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get(url: str, *, timeout: float = 120.0) -> Tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post_form(url: str, fields: Dict[str, str]) -> Tuple[int, str, Dict[str, str]]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def entry_forms(page: str) -> List[Dict[str, str]]:
    """页面上每个打标表单的 hidden 字段（按 `<form>` 精确切分）。"""

    class _Forms(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.forms: List[Dict[str, str]] = []
            self.current: Optional[Dict[str, str]] = None

        def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
            attributes = dict(attrs)
            if tag == "form":
                self.current = {} if attributes.get("action") == "/label" else None
                if self.current is not None:
                    self.forms.append(self.current)
                return
            if tag == "input" and self.current is not None:
                if attributes.get("type") == "hidden":
                    self.current[attributes.get("name", "")] = attributes.get("value", "")

        def handle_endtag(self, tag: str) -> None:  # noqa: ANN001
            if tag == "form":
                self.current = None

    parser = _Forms()
    parser.feed(page)
    return parser.forms


def titles(page: str) -> List[Tuple[str, str]]:
    return re.findall(r'<a class="title" href="([^"]*)"[^>]*>(.*?)</a>', page, re.DOTALL)


# ---------------------------------------------------------------------- #
# 步骤
# ---------------------------------------------------------------------- #
def step_cli(store_root: Path, actor: str, port: int) -> Tuple[subprocess.Popen, str]:
    """1) 用一条命令把服务起起来，并返回 (进程, 启动输出)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    command = [
        str(REPO_ROOT / ".venv-new" / "bin" / "python"),
        "-m",
        "atlas.webapp",
        "--store-root",
        str(store_root),
        "--port",
        str(port),
        "--actor",
        actor,
    ]
    print("[命令]", " ".join(command))
    process = subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 300
    buffer = ""
    while time.time() < deadline:
        line = process.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if process.poll() is not None:
                raise SystemExit(f"CLI 提前退出（码 {process.returncode}）：\n{buffer}")
            time.sleep(0.1)
            continue
        buffer += line
        # 启动横幅有多行（URL / 计数 / 退出方式），必须**读完整**再断言，
        # 否则会漏掉后半段（实测踩过：只读到 URL 行就 break，"Ctrl-C" 断言假失败）。
        if "Ctrl-C" in buffer:
            break
    else:
        process.kill()
        raise SystemExit(f"CLI 启动超时。输出：\n{buffer}")
    return process, buffer


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store-root", default=str(DEFAULT_STORE_ROOT))
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument("--port", type=int, default=0, help="0 = 自动挑一个空闲端口")
    parser.add_argument("--label-key", default="industry")
    parser.add_argument(
        "--label-value",
        default=None,
        help="默认：取该条目所属渠道的行业（一个**客观**维度：把它修正成它本就属于的行业）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证 CLI 启动 + GET /feed，不写任何标签",
    )
    args = parser.parse_args(argv)

    store_root = Path(args.store_root).resolve()
    db_path = store_root / "atlas.db"
    if not db_path.is_file():
        raise SystemExit(f"真实存储不存在：{db_path}")

    tags_before = _count_labels(db_path)
    print(f"运行前 confirmed_labels 行数：{tags_before}")

    port = args.port or free_port()
    process, startup = step_cli(store_root, args.actor, port)
    url = f"http://127.0.0.1:{port}"
    try:
        print("---- CLI 实际启动输出 ----")
        print(startup.rstrip())
        print("-------------------------")
        assert f"http://127.0.0.1:{port}" in startup, "启动输出里必须有真实 URL"
        assert "Ctrl-C" in startup, "启动输出里必须说明怎么退出"

        # 2) GET /feed
        status, page = get(url + "/feed?limit=5")
        print(f"GET /feed → {status}")
        assert status == 200, page[:400]
        shown = titles(page)
        assert shown, "HTML 里必须有条目标题"
        print("HTML 里的前几条条目（标题 / 链接）：")
        for link, title in shown[:5]:
            print(f"  - {title.strip()[:90]}\n    {link}")
        assert any(not title.strip().startswith("raw_") for _l, title in shown)

        forms = entry_forms(page)
        assert forms, "页面里必须有打标表单"
        # 优先选**容器派生**的条目（区间不是整篇 `[0, len)`）——那才是 T-109 的主场景：
        # 一份 feed 里的一篇文章。整篇条目（旧语料 / 非 feed 记录）作为兜底。
        target = next((f for f in forms if f.get("char_start") not in (None, "0")), forms[0])
        print(
            "选中要打标的条目："
            f"raw_id={target['raw_id']} span=[{target['char_start']},{target['char_end']})"
        )

        if args.dry_run:
            print("--dry-run：不写标签，结束。")
            return 0

        # 3) POST /label（带锚点）
        label_value = args.label_value
        if label_value is None:
            label_value = _industry_of(db_path, target["raw_id"])
            print(
                f"（--label-value 未给出 ⇒ 用该条目所属渠道**配置里的行业**：{label_value}；"
                "这是一个**客观**取值（来自 config），不是对内容的主观判断）"
            )
        fields = {
            "raw_id": target["raw_id"],
            "label_key": args.label_key,
            "label_value": label_value,
            "actor": args.actor,
            "raw_sha256": target["raw_sha256"],
            "char_start": target["char_start"],
            "char_end": target["char_end"],
            "return_to": "/feed",
        }
        status, body, headers = post_form(url + "/label", fields)
        print(f"POST /label → {status}（Location: {headers.get('Location')}）")
        assert status == 303, (status, body[:400])

        # 4) 独立 sqlite3 复核
        rows = _read_label_rows(
            db_path,
            raw_id=target["raw_id"],
            label_key=args.label_key,
            label_value=label_value,
            actor=args.actor,
        )
        print("独立 sqlite3 复核（不经过任何前端代码）：")
        for row in rows:
            print("  " + json.dumps(row, ensure_ascii=False))
        assert rows, "标签没有落库"
        row = rows[-1]
        assert row["anchor_char_start"] is not None and row["anchor_char_end"] is not None
        start, end = int(target["char_start"]), int(target["char_end"])
        assert row["anchor_char_start"] == start and row["anchor_char_end"] == end, (
            "落库的锚点区间与条目区间不一致"
        )
        assert 0 <= row["anchor_char_start"] < row["anchor_char_end"]
        print(
            f"✅ 锚点 [{row['anchor_char_start']}, {row['anchor_char_end']}) "
            f"非空且等于该条目区间"
        )

        # 状态回显
        status, page2 = get(url + "/feed?limit=5")
        assert status == 200
        tag = f'<span class="label-tag">{args.label_key}={label_value}</span>'
        assert tag in page2, "打标后页面必须回显该标签"
        print(f"✅ 再次 GET /feed 回显：{tag}")

        # 条目页高亮
        query = urllib.parse.urlencode(
            {
                "raw_id": target["raw_id"],
                "char_start": target["char_start"],
                "char_end": target["char_end"],
            }
        )
        status, detail = get(url + "/entry?" + query)
        assert status == 200 and "<mark>" in detail
        print("✅ GET /entry → 200 且含 <mark>（高亮该条目区间）")

        after = _count_labels(db_path)
        print(f"运行后 confirmed_labels 行数：{after}（新增 {after - tags_before}）")
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
    return 0


def _count_labels(db_path: Path) -> int:
    connection = sqlite3.connect(str(db_path))
    try:
        return int(connection.execute("SELECT COUNT(*) FROM confirmed_labels").fetchone()[0])
    finally:
        connection.close()


def _read_label_rows(
    db_path: Path, *, raw_id: str, label_key: str, label_value: str, actor: str
) -> List[Dict[str, Any]]:
    """**独立打开 sqlite3** 读那一行（不经 atlas.labels、不经前端）。"""
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT label_id, raw_id, label_key, label_value, actor, "
            "anchor_raw_id, anchor_raw_sha256, anchor_char_start, anchor_char_end, created_at "
            "FROM confirmed_labels WHERE raw_id = ? AND label_key = ? "
            "AND label_value = ? AND actor = ? ORDER BY rowid",
            (raw_id, label_key, label_value, actor),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _industry_of(db_path: Path, raw_id: str) -> str:
    """该 raw 所属渠道的行业（从 `raw_records` + `channels` 读，一个客观维度）。

    `channels` 表的键列名是 **`channel_id`**（不是 `id`；实测自真实库的
    `PRAGMA table_info(channels)`）—— 表归属 T-101，这里只读。
    """
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute(
            "SELECT c.industry_id FROM raw_records r "
            "JOIN channels c ON c.channel_id = r.channel_id WHERE r.raw_id = ?",
            (raw_id,),
        ).fetchone()
        if row is None or not row[0]:
            raise SystemExit(f"渠道配置里找不到 {raw_id} 对应的行业，请显式给 --label-value")
        return str(row[0])
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
