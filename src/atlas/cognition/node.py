"""T-003 解析可用的 `node` 可执行文件（SPEC §6 记录的 WSL 坑）。

**实测事实（本机 WSL Ubuntu-24.04，2026-09-26）**

| 路径 | 结果 |
|---|---|
| `node`（裸名，走 `PATH`） | ❌ **失败**。`PATH` 首位是 DSH 自带的占位文件
  `…/DSH Desktop/resources/app/node_modules/node/bin/node`，内容只有一行
  `This file intentionally left blank`，执行即 `command not found` |
| `npm`（裸名） | ✅ 正常，解析到 `/mnt/c/Program Files/nodejs/npm`（Windows 侧，v24.11.0） |
| `~/.nvm/versions/node/v22.21.1/bin/node` | ✅ 正常，**v22.21.1** |
| `/usr/bin/node`、`/usr/local/bin/node` | ❌ 不存在 |

结论：**不能信任裸名 `node`**，必须逐个候选验证（执行 `--version` 并检查最低版本）。

因此本模块做三件事，且**绝不硬编码机器专有路径而没有回退或明确报错**：

1. 显式配置优先（`CognitionConfig.node_bin`）；
2. `PATH` 逐目录查找，逐个验证——**未通过验证的候选不被采用**；
3. 已知的版本管理器布局 + 常见系统路径，逐个验证；
4. 全部失败时抛 `SidecarUnavailableError`，消息里**列出所有候选与失败原因**，
   并给出可执行的修复建议。

最低版本 `22.3.0` 来自实际依赖：边车用了 `process.getBuiltinModule`（Node ≥ 22.3）
与顶层 `await` + ESM。版本不足的候选会被跳过，而不是"试试看"。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .errors import SidecarUnavailableError

__all__ = ["MINIMUM_NODE_VERSION", "NodeBinary", "mini_version", "resolve_node"]

MINIMUM_NODE_VERSION = (22, 3, 0)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PROBE_TIMEOUT_SECONDS = 20.0

#: 已知的版本管理器布局与系统路径（`*` 由 glob 展开，按版本号降序尝试）。
_KNOWN_GLOBS: Tuple[str, ...] = (
    "~/.nvm/versions/node/*/bin/node",
    "~/.local/share/fnm/node-versions/*/installation/bin/node",
    "~/.fnm/node-versions/*/installation/bin/node",
    "~/.volta/bin/node",
    "~/.asdf/installs/nodejs/*/bin/node",
    "~/.local/share/mise/installs/node/*/bin/node",
    "~/.nodenv/versions/*/bin/node",
)
_KNOWN_PATHS: Tuple[str, ...] = (
    "/usr/local/bin/node",
    "/usr/bin/node",
    "/bin/node",
    "/opt/node/bin/node",
)


@dataclass(frozen=True)
class NodeBinary:
    """已**验证可用**的 node。`source` 说明它是怎么被找到的（可审计）。"""

    path: str
    version: str
    source: str

    @property
    def version_tuple(self) -> Tuple[int, int, int]:
        return mini_version(self.version)

    def describe(self) -> str:
        return f"{self.path} ({self.version}, via {self.source})"


def mini_version(version: str) -> Tuple[int, int, int]:
    """把 `v22.21.1` / `22.21.1` 解析成 `(22, 21, 1)`；不可解析抛错。"""
    match = _VERSION_RE.search(version)
    if match is None:
        raise SidecarUnavailableError(f"无法解析 node 版本号：{version!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _expand(pattern: str) -> List[Path]:
    if pattern.startswith("~/"):
        return sorted(Path.home().glob(pattern[2:]), reverse=True)
    return sorted(Path("/").glob(pattern.lstrip("/")), reverse=True)


def _candidate_paths(configured: str) -> List[Tuple[str, str]]:
    """返回 `(路径, 来源说明)` 列表，**已去重并保序**。"""
    seen: set[str] = set()
    ordered: List[Tuple[str, str]] = []

    def add(path: str, source: str) -> None:
        if not path:
            return
        key = os.path.abspath(path)
        if key in seen:
            return
        seen.add(key)
        ordered.append((path, source))

    if configured:
        add(configured, "CognitionConfig.node_bin")

    # PATH 逐目录查找（不信任裸名 `node` 的结果，所以自己拼路径再验证）。
    path_env = os.environ.get("PATH", "")
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        found = shutil.which("node", path=directory)
        if found:
            add(found, f"PATH:{directory}")

    for pattern in _KNOWN_GLOBS:
        for match in _expand(pattern):
            add(str(match), "known-layout")
    for path in _KNOWN_PATHS:
        add(path, "system-path")

    return ordered


def _probe(path: str) -> Tuple[Optional[str], str]:
    """执行 `--version`；返回 `(版本或 None, 失败原因)`。"""
    if not os.path.isfile(path):
        return None, "不是常规文件"
    if not os.access(path, os.X_OK):
        return None, "没有执行权限"
    try:
        completed = subprocess.run(  # noqa: S603 - 路径来自受控候选表，shell=False
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"执行失败：{type(exc).__name__}: {exc}"
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return None, f"退出码 {completed.returncode}，输出 {output[:80]!r}"
    match = _VERSION_RE.search(output)
    if match is None:
        return None, f"输出不含版本号：{output[:80]!r}"
    return match.group(0), ""


def resolve_node(
    configured: str = "",
    *,
    minimum: Sequence[int] = MINIMUM_NODE_VERSION,
    candidates: Optional[Sequence[Tuple[str, str]]] = None,
) -> NodeBinary:
    """找到并验证一个可用的 node；找不到就**响亮失败**。

    `candidates` 只用于测试注入（模拟"全是坏的候选"）。
    """
    minimum_tuple = tuple(minimum)
    attempts: List[str] = []
    best: Optional[NodeBinary] = None

    for path, source in candidates if candidates is not None else _candidate_paths(configured):
        version, failure = _probe(path)
        if version is None:
            attempts.append(f"  · {path}（{source}）：{failure}")
            continue
        parsed = mini_version(version)
        if parsed < minimum_tuple:
            attempts.append(
                f"  · {path}（{source}）：版本 {version} < 要求 "
                f"{'.'.join(str(part) for part in minimum_tuple)}"
            )
            continue
        return NodeBinary(path=os.path.abspath(path), version=version, source=source)

    message = [
        "找不到可用的 node 可执行文件（认知层边车无法启动）。",
        f"要求 >= {'.'.join(str(part) for part in minimum_tuple)}；已尝试：",
        *attempts,
    ]
    if best is not None:  # pragma: no cover - 保留给将来"降级到旧版本"的策略
        message.append(f"可用但版本不足：{best.describe()}")
    message.append(
        "修复方式：① 设置 CognitionConfig(node_bin=...) 指向可用的 node；"
        "或 ② 把可用的 node 目录加进 PATH；"
        "或 ③ 安装 Node.js >= 22.3（例如 `nvm install 22`）。"
        "注意 WSL 内裸名 `node` 可能解析到 DSH 的占位文件（SPEC §6）。"
    )
    raise SidecarUnavailableError("\n".join(message))
