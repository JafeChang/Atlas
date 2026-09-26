"""测试辅助：以**子进程**方式调用 `python -m atlas.compose`（离线，不打网络）。

为什么是共享模块而不是每个测试文件各写一份：T-120 的
`tests/test_compose_pipeline.py` 与 T-207 的 `tests/test_compose_cli_schedule.py`
都需要一个"干净、无 `ATLAS_LIVE`、`PYTHONPATH` 指向本检出"的子进程环境。
两份实现会漂移，而漂移的那一份会让"CLI 真的能跑"这条证据失真。

文件名用 `_` 前缀：它**不是**测试模块（pytest 不该收集它，见 CLAUDE.md 的测试约定）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

__all__ = ["REPO_ROOT", "run_cli"]


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """跑一次 `python -m atlas.compose <args>`；默认**不**带 `ATLAS_LIVE`。"""
    environment = dict(os.environ)
    environment.pop("ATLAS_LIVE", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "atlas.compose", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=environment,
        timeout=120,
    )
