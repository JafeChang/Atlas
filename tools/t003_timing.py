"""T-003 / T-105 预算测量：每次认知层调用的墙钟时间花在哪里。

跑法（WSL 内）：

    ./.venv-new/bin/python tools/t003_timing.py

结论（2026-09-26 实测）：

| 项 | 时间 |
|---|---|
| `node` 启动本身 | ~12 ms |
| `import '@earendil-works/pi-ai'`（含 barre）—— **在 `/mnt/c`（drvfs）上** | ~5900–6400 ms |
| 同上，把边车 + `node_modules` 放到 **原生 Linux 文件系统** | **87 ms** |
| `import 'openai'`（barrel 的传递依赖之一） | ~1000 ms |
| 边车 `inspect` 一次往返（无模型调用），`/mnt/c` | ~3600 ms |

也就是说：**成本几乎全部是 drvfs 上逐文件读取 node_modules 的开销**，不是 pi-ai 本身慢。
因为边车是"一次调用一个进程"，这笔开销**每次调用都要付**。

对 T-105 的直接影响：批量分类 N 篇文档时，进程启动成本是 `N × ~3.6 s`。
两条可行的缓解（**都不需要改协议**）：

1. 把 `node_modules` 放在原生 Linux 文件系统上，用符号链接接进来（实测降到 ~90 ms）；
2. 若仍不够，再把边车改成常驻进程 + 多 job（协议已是"一行一个 job"，扩展成本低）。

本脚本只测量、不改设计——把数字留在这里，让 T-105 自己决策。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = REPO_ROOT / "src" / "atlas" / "cognition" / "sidecar"
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlas.cognition import CognitionConfig, PiSidecarCognitionPort, resolve_node  # noqa: E402

IMPORT_PROBES = [
    ("node boot only", "0"),
    ("import '@earendil-works/pi-ai' (barrel)", "await import('@earendil-works/pi-ai');"),
    ("import 'openai'", "await import('openai');"),
    (
        "import lazy completions api only",
        "await import('@earendil-works/pi-ai/api/openai-completions.lazy');",
    ),
]


def main() -> int:
    node = resolve_node()
    print(f"node: {node.describe()}")
    print(f"sidecar dir (on drvfs?): {SIDECAR}\n")

    print(f"{'probe':<44}{'wall (ms)':>10}")
    print("-" * 54)
    for label, expression in IMPORT_PROBES:
        started = time.monotonic()
        subprocess.run(
            [node.path, "-e", expression],
            cwd=str(SIDECAR),
            capture_output=True,
            check=False,
        )
        elapsed = (time.monotonic() - started) * 1000
        print(f"{label:<44}{elapsed:>10.0f}")

    print()
    port = PiSidecarCognitionPort(
        CognitionConfig(
            provider="timing",
            model="timing",
            base_url="http://127.0.0.1:1/v1",
            api_key="not-a-secret",
        )
    )
    print("sidecar inspect round trip (no model call):")
    for label in ("run 1", "run 2", "run 3"):
        started = time.monotonic()
        report = port.inspect()
        elapsed = (time.monotonic() - started) * 1000
        print(f"  {label}: {elapsed:>7.0f} ms  (toolsRegistered={report['toolsRegistered']})")

    print(
        "\n对照实验（原生文件系统）：把 sidecar + node_modules 复制到 /tmp 后\n"
        "  barrel import = 87 ms，而 /mnt/c 上是 ~6000 ms。\n"
        "  ⇒ 成本来自 drvfs 逐文件读取，不是 pi-ai 本身。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
