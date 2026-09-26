"""T-003 PI 边车适配器：`CognitionPort` 的一个实现（SPEC §2.14 决策二）。

**为什么是适配器而不是"认知层本体"**

`CognitionPort` 定义稳定接口；本模块只是它的**一个**实现。换 provider / 换模型 /
换回直连 HTTP / 换成别的语言写的边车，都只换这一个类，调用方不动。

**凭据怎么进边车（重要）**

密钥**不经过环境变量**。它随 job 载荷（stdin 上的一行 JSON）进入子进程，随后只活在
`run.mjs` 的闭包里；子进程的环境被重建为最小集合（`PATH` / `HOME` / …）。
因此 a) 子进程里的任何模块都读不到密钥；b) 被注入的指令即便让模型输出
"执行 env" 也没有执行面；c) 密钥不会被写进任何日志/记录。

**IPC 协议**（版本化：`atlas.cognition.sidecar/1`，常量见 `PROTOCOL`）

* 父 → 子：stdin 上 **一行** JSON job；子进程读完即开始处理（一次性进程，无长连接状态）；
* 子 → 父：**数据通道**（父进程打开的 fd 3，未开则退回 stdout）上的 **帧**行，
  每行形如 `#atlas-cognition/1#{...}`；
* 数据通道上**所有不以帧前缀开头的行一律忽略**——这是对"库往 stdout 打警告"的真实防御，
  有专门测试把它钉死（`test_cognition_protocol.py`）；
* 退出码：0 = 全部 job 产出了结果帧（含已处理的降级）；2 = 有 job 被判为协议/内部错误；1 = 崩溃。
* 模型输出**永不**被当作代码：不走 `eval` / `exec` / shell，不拼命令，不写文件。
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import CognitionConfig
from .contracts import (
    CallStatus,
    CognitionCallRecord,
    CognitionOutput,
    CognitionRequest,
    CognitionResult,
    CognitionUsage,
    DegradeReason,
    canonical_digest,
)
from .errors import (
    CognitionPortError,
    ConfigError,
    IsolationViolationError,
    ModelOutputError,
    ProtocolError,
    SidecarUnavailableError,
)
from .node import NodeBinary, resolve_node
from .parse import extract_single_json
from .prompt import PROMPT_VERSION, build_system_prompt, build_user_content, truncate_content

__all__ = ["PROTOCOL", "FRAME_PREFIX", "SIDECAR_RELATIVE_PATH", "PiSidecarCognitionPort"]

#: 协议版本。**改动帧结构必须同步升版**，两侧按版本号拒绝。
PROTOCOL = "atlas.cognition.sidecar/1"
#: 帧前缀。它是"这行是协议数据"的唯一判据；其余行全是噪声。
FRAME_PREFIX = "#atlas-cognition/1#"

SIDECAR_RELATIVE_PATH = Path("sidecar") / "run.mjs"
_PACKAGE_ROOT = Path(__file__).resolve().parent
_DEFAULT_WALL_CLOCK_MARGIN_SECONDS = 20.0

#: 边车 reason 码 → 本包枚举。**闭集**：收到未知码按 `sidecar_error` 记录并保留原文，
#: 绝不静默丢弃（`detail` 里带着原始码）。
_REASON_MAP: Dict[str, DegradeReason] = {
    "unreachable_model": DegradeReason.UNREACHABLE_MODEL,
    "timeout": DegradeReason.TIMEOUT,
    "transport_timeout": DegradeReason.TIMEOUT,
    "transport_error": DegradeReason.UNREACHABLE_MODEL,
    "http_error": DegradeReason.HTTP_ERROR,
    "model_deprecated": DegradeReason.MODEL_DEPRECATED,
    "empty_completion": DegradeReason.EMPTY_COMPLETION,
    "unparseable_output": DegradeReason.UNPARSEABLE_OUTPUT,
    "sidecar_error": DegradeReason.SIDECAR_ERROR,
    "protocol_error": DegradeReason.SIDECAR_ERROR,
}

#: 模型下架时响应体里的特征词（SPEC §2.14 决策三第 1 类的实测证据）。
_DEPRECATED_MARKERS = ("deprecated", "has been deprecated", "no longer available", "decommissioned")


class PiSidecarCognitionPort:
    """`CognitionPort` 的 pi-ai 边车实现。

    参数
    ----
    config
        调用配置（provider / model / 凭据 / 超时 / 代理 …）。
    node
        已解析的 node；`None` 则在第一次调用时惰性解析（避免构造期就依赖本机环境）。
    cache
        `True` 时按幂等键缓存结果：同输入 + 同配置 → **直接返回旧结果，不再调用模型**。
        这是"幂等"的可操作定义（SPEC §3）。默认关闭，避免隐式行为。
    """

    def __init__(
        self,
        config: CognitionConfig,
        *,
        node: Optional[NodeBinary] = None,
        cache: bool = False,
        sidecar_path: Optional[Path] = None,
        require_installed: bool = True,
    ) -> None:
        self.config = config
        self._node = node
        self._cache_enabled = cache
        self._cache: Dict[str, CognitionResult] = {}
        self._records: List[CognitionCallRecord] = []
        self._sidecar_path = Path(sidecar_path) if sidecar_path else _PACKAGE_ROOT / SIDECAR_RELATIVE_PATH
        self._sidecar_sha256 = ""
        self._require_installed = require_installed

    # ------------------------------------------------------------------ #
    # 只读视图
    # ------------------------------------------------------------------ #

    @property
    def records(self) -> Tuple[CognitionCallRecord, ...]:
        """按时间顺序的全部调用记录（只读快照）。"""
        return tuple(self._records)

    @property
    def sidecar_sha256(self) -> str:
        return self._sidecar_sha256

    def node_binary(self) -> NodeBinary:
        if self._node is None:
            self._node = resolve_node(self.config.node_bin)
        return self._node

    # ------------------------------------------------------------------ #
    # 环境 / 前置检查
    # ------------------------------------------------------------------ #

    def sidecar_env(self) -> Dict[str, str]:
        """子进程的**初始**环境：只传它需要的最小集合，不继承整份宿主环境。

        两条规则合起来才构成"最小环境"：

        1. **默认什么都不传**：只有下面显式列出的键进入子进程；
        2. **白名单是唯一的例外通道**：`host_env_allowlist` 里的变量名会被
           **逐个从宿主环境取值**后放进子进程（例如需要 `HTTPS_PROXY` 时）。
           白名单为容器时，子进程看到的宿主变量数为 **0**。

        注意密钥**根本不在这里**：它随 job 载荷走 stdin，子进程环境里没有任何密钥形变量。
        """
        env = {
            "ATLAS_COGNITION_HOST_ENV": ",".join(self.config.host_env_allowlist),
            # 边车需要 PATH 才能被 node 自身解析（node 已由父进程绝对路径给出）。
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            # 全局 fetch 只在 Node 启动前看到该开关时才挂上 EnvHttpProxyAgent。
            "NODE_USE_ENV_PROXY": "1",
            "NODE_NO_WARNINGS": "1",
        }
        home = os.environ.get("HOME")
        if home:
            env["HOME"] = home
        for name in self.config.host_env_allowlist:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        if self.config.proxy_url:
            env.setdefault("HTTPS_PROXY", self.config.proxy_url)
            env.setdefault("HTTP_PROXY", self.config.proxy_url)
        return env

    def preflight(self) -> NodeBinary:
        """在**不调用模型**的前提下确认边车可用（node 在、依赖在）。

        用于运维/测试的显式检查；`extract` 自己也会调用它。
        """
        node = self.node_binary()
        if not self._sidecar_path.is_file():
            raise SidecarUnavailableError(
                f"边车源码不存在：{self._sidecar_path}"
            )
        modules = self._sidecar_path.parent / "node_modules"
        if self._require_installed and not modules.is_dir():
            raise SidecarUnavailableError(
                "边车依赖未安装："
                f"{modules} 不存在。请在该目录执行 "
                "`npm install --no-audit --no-fund`（需要走代理时先导出 HTTPS_PROXY）。"
            )
        return node

    # ------------------------------------------------------------------ #
    # 幂等键
    # ------------------------------------------------------------------ #

    def idempotency_key(self, request: CognitionRequest) -> str:
        """`(输入摘要, 配置摘要, 版本三元组)` 的规范化摘要（SPEC §3）。"""
        versions = self.config.versions()
        return canonical_digest(
            {
                "input_digest": request.digest(),
                "config_digest": self.config.public_digest(),
                "code_version": versions["code_version"],
                "config_version": versions["config_version"],
                "model_version": versions["model_version"],
            }
        )

    # ------------------------------------------------------------------ #
    # 端口实现
    # ------------------------------------------------------------------ #

    def extract(self, request: CognitionRequest) -> CognitionResult:
        """一次最小结构化调用。

        返回 `CognitionResult`；**降级时返回 `unclassified` + 原因码**，
        契约违例 / 环境问题则抛异常（响亮失败）。
        """
        if not request.external_content:
            raise CognitionPortError("external_content 不得为空")

        key = self.idempotency_key(request)
        if self._cache_enabled and key in self._cache:
            return self._cache[key]

        node = self.preflight()
        content, dropped = truncate_content(request.external_content, self.config.max_context_chars)
        system_prompt = build_system_prompt(request.candidate_labels, request.kind)
        user_content = build_user_content(request.instruction, content)

        job = self._build_job(request, key, node, system_prompt, user_content)
        outcome = self._invoke(job, node)
        result = self._to_result(request, key, outcome, dropped)
        self._records.append(result.record)
        if self._cache_enabled:
            self._cache[key] = result
        return result

    def __call__(self, request: CognitionRequest) -> CognitionResult:
        return self.extract(request)

    # ------------------------------------------------------------------ #
    # job 组装
    # ------------------------------------------------------------------ #

    def _build_job(
        self,
        request: CognitionRequest,
        key: str,
        node: NodeBinary,
        system_prompt: str,
        user_content: str,
    ) -> Dict[str, Any]:
        config = self.config
        call: Dict[str, Any] = {
            "provider": config.provider,
            "model": config.model,
            "baseUrl": config.base_url,
            "timeoutMs": int(config.timeout_seconds * 1000),
            "maxOutputTokens": config.max_output_tokens,
            "temperature": config.temperature,
            "reasoning": config.reasoning_enabled,
            "noProxy": config.no_proxy,
            "systemPrompt": system_prompt,
            "userContent": user_content,
        }
        if config.api_key:
            call["apiKey"] = config.api_key
        if config.proxy_url:
            call["proxyUrl"] = config.proxy_url
        return {
            "protocol": PROTOCOL,
            "jobId": f"cog-{uuid.uuid4().hex[:16]}",
            "operation": "call",
            "idempotencyKey": key,
            "hostEnvAllowlist": list(config.host_env_allowlist),
            "call": call,
        }

    # ------------------------------------------------------------------ #
    # 子进程往返
    # ------------------------------------------------------------------ #

    def _invoke(self, job: Mapping[str, Any], node: NodeBinary) -> "_Outcome":
        limit = self.config.timeout_seconds + _DEFAULT_WALL_CLOCK_MARGIN_SECONDS
        started = time.monotonic()
        with tempfile.TemporaryFile("w+b") as stderr_log:
            try:
                process = subprocess.Popen(  # noqa: S603 - argv 全部由本模块构造，shell=False
                    [node.path, str(self._sidecar_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_log,
                    env=self.sidecar_env(),
                    cwd=str(self._sidecar_path.parent),
                    text=False,
                )
            except OSError as exc:
                raise SidecarUnavailableError(
                    f"无法启动边车进程：{node.describe()} → {type(exc).__name__}: {exc}"
                ) from exc

            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(job, ensure_ascii=False).encode("utf-8") + b"\n")
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                process.kill()
                process.wait(timeout=10)
                raise ProtocolError(f"向边车写 job 失败：{type(exc).__name__}: {exc}") from exc

            assert process.stdout is not None
            deadline = started + limit
            noise: List[str] = []
            frames: List[Dict[str, Any]] = []
            chunks: List[bytes] = []
            timed_out = False
            # 看门狗用 `select` 超时实现，**不能**把管道设为非阻塞：管道两端的
            # file status flags 是同一份，父进程设 O_NONBLOCK 会让子进程的
            # writeSync 直接 EINVAL。
            stdout_fd = process.stdout.fileno()
            try:
                while True:
                    ready, _, _ = select.select([stdout_fd], [], [], 0.05)
                    if ready:
                        chunk = os.read(stdout_fd, 65536)
                        if chunk:
                            chunks.append(chunk)
                            continue
                        break  # EOF：子进程已关闭 stdout
                    if process.poll() is not None:
                        chunks.append(os.read(stdout_fd, 65536))
                        break
                    if time.monotonic() > deadline:
                        process.kill()
                        process.wait(timeout=10)
                        timed_out = True
                        break
            except OSError as exc:
                if process.poll() is None:
                    raise ProtocolError(
                        f"读取边车数据通道失败：{type(exc).__name__}: {exc}"
                    ) from exc

            for line in b"".join(chunks).decode("utf-8", errors="replace").splitlines():
                if line.startswith(FRAME_PREFIX):
                    try:
                        frames.append(json.loads(line[len(FRAME_PREFIX) :]))
                    except ValueError as exc:
                        raise ProtocolError(f"边车帧不是合法 JSON：{exc}") from exc
                elif line.strip():
                    # 数据通道上的非帧内容一律忽略——这是对"库往 stdout 打警告"的防御。
                    noise.append(line)

            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 已 kill 的情况
                process.kill()
                return_code = process.wait(timeout=10)

            elapsed_ms = int((time.monotonic() - started) * 1000)
            stderr_log.seek(0)
            diagnostics = stderr_log.read().decode("utf-8", errors="replace")

            if timed_out:
                return _Outcome(
                    frame=None,
                    elapsed_ms=elapsed_ms,
                    return_code=return_code,
                    noise=tuple(noise),
                    diagnostics=diagnostics,
                    watchdog_timeout=True,
                )
            if not frames:
                raise ProtocolError(
                    f"边车没有产出任何协议帧（退出码 {return_code}）。"
                    f"\n噪声行：{noise[:5]!r}"
                    f"\n子进程输出：{diagnostics[-2000:]!r}"
                )
            if len(frames) > 1:
                raise ProtocolError(f"边车对单个 job 产出了 {len(frames)} 个帧，预期 1 个")

            frame = frames[0]
            self._sidecar_sha256 = str(frame.get("sidecarSha256") or "")
            self._check_frame(frame)
            return _Outcome(
                frame=frame,
                elapsed_ms=elapsed_ms,
                return_code=return_code,
                noise=tuple(noise),
                diagnostics=diagnostics,
            )

    @staticmethod
    def _check_frame(frame: Mapping[str, Any]) -> None:
        if frame.get("protocol") != PROTOCOL:
            raise ProtocolError(
                f"边车协议版本不符：期望 {PROTOCOL}，收到 {frame.get('protocol')!r}"
            )
        if "error" in frame:
            error = frame["error"] or {}
            code = error.get("code", "internal_error")
            if code == "unsupported_operation":
                raise ProtocolError(f"边车不支持该操作：{error.get('message')}")
            if code == "protocol_mismatch":
                raise ProtocolError(f"边车协议校验失败：{error.get('message')}")
            raise ProtocolError(f"边车内部错误 [{code}]：{error.get('message')}")
        if "result" not in frame:
            raise ProtocolError("边车帧缺少 result 字段")
        # 隔离是**结构性的**：边车必须报告注册了零工具。非零即隔离被破坏，
        # 这属于接线/实现缺陷，必须响亮失败而不是降级（SPEC §4.7）。
        if "toolsRegistered" in frame and int(frame["toolsRegistered"]) != 0:
            raise IsolationViolationError(
                f"边车报告注册了 {frame['toolsRegistered']} 个工具，"
                "而 SPEC §2.14 决策一要求**零工具**——隔离前提已不成立。"
            )

    # ------------------------------------------------------------------ #
    # 帧 → 结果
    # ------------------------------------------------------------------ #

    def _to_result(
        self,
        request: CognitionRequest,
        key: str,
        outcome: "_Outcome",
        dropped_chars: int,
    ) -> CognitionResult:
        common_identity = {
            "provider": self.config.provider,
            "model": self.config.model,
            "credential_route": self.config.credential_route(),
            "sidecar_code_version": self._sidecar_sha256,
            "code_version": self.config.prompt_version,
            "config_version": self.config.config_version,
            "model_version": self.config.model_version,
            "config_digest": self.config.public_digest(),
            "idempotency_key": key,
            "input_digest": request.digest(),
        }

        if outcome.watchdog_timeout:
            # 看门狗超时 = 模型侧拿不到输出 ⇒ **降级**（不是响亮失败）。
            return self._degraded(
                request,
                {
                    **common_identity,
                    "response_model": "",
                    "elapsed_ms": outcome.elapsed_ms,
                    "usage": CognitionUsage(),
                    "tool_calls": 0,
                    "tools_declared": 0,
                    "thinking_chars": 0,
                },
                DegradeReason.TIMEOUT,
                (
                    f"边车看门狗超时（{self.config.timeout_seconds + _DEFAULT_WALL_CLOCK_MARGIN_SECONDS:.1f}s），"
                    f"已 kill 子进程；子进程输出尾部：{outcome.diagnostics[-300:]!r}"
                ),
                None,
            )

        frame: Mapping[str, Any] = outcome.frame or {}
        raw = frame.get("result") or {}
        status = str(raw.get("status", ""))
        tools_declared = int(frame.get("toolsRegistered", raw.get("tools_declared", -1)))
        common = {
            **common_identity,
            "provider": str(raw.get("provider") or self.config.provider),
            "model": str(raw.get("model") or self.config.model),
            "response_model": str(raw.get("response_model") or ""),
            "elapsed_ms": int(raw.get("elapsed_ms") or outcome.elapsed_ms),
            "usage": _usage_from(raw.get("usage")),
            "tool_calls": int(raw.get("tool_calls") or 0),
            "tools_declared": tools_declared,
            "thinking_chars": int(raw.get("thinking_chars") or 0),
        }

        if status == "text":
            text = str(raw.get("text") or "")
            try:
                extracted = extract_single_json(text)
            except ModelOutputError as exc:
                # 「拿不到可用的模型输出」⇒ 降级为未分类（SPEC §2.14 决策四）。
                return self._degraded(
                    request,
                    common,
                    DegradeReason.UNPARSEABLE_OUTPUT,
                    _detail(exc, dropped_chars),
                    parse_strategy=None,
                )
            output = CognitionOutput.parse(extracted.value)  # 契约违例 ⇒ 响亮失败
            record = CognitionCallRecord(
                status=CallStatus.OK,
                reason=None,
                detail=_detail(None, dropped_chars),
                parse_strategy=extracted.strategy,
                degraded=False,
                **common,
            )
            return CognitionResult(record=record, output=output)

        if status == "degraded":
            reason_code = str(raw.get("reason") or "sidecar_error")
            if reason_code == "config_error":
                raise ConfigError(f"边车报告配置错误：{raw.get('detail')}")
            reason = _REASON_MAP.get(reason_code)
            detail = str(raw.get("detail") or raw.get("error_message") or "")
            if reason_code == "http_error" and _looks_deprecated(detail):
                # SPEC §2.14 决策三第 1 类的实测形态：404 + "has been deprecated"。
                reason = DegradeReason.MODEL_DEPRECATED
            if reason is None:
                # 未知码不丢：归到 sidecar_error，原始码留在 detail 里。
                detail = f"unmapped_reason={reason_code}; {detail}"
                reason = DegradeReason.SIDECAR_ERROR
            return self._degraded(
                request, common, reason, _detail_detail(detail, dropped_chars), None
            )

        raise ProtocolError(f"边车返回了未知 status：{status!r}（原始帧：{raw!r}）")

    def _degraded(
        self,
        request: CognitionRequest,
        common: Mapping[str, Any],
        reason: DegradeReason,
        detail: str,
        parse_strategy: Optional[str],
    ) -> CognitionResult:
        record = CognitionCallRecord(
            status=CallStatus.UNCLASSIFIED,
            reason=reason,
            detail=detail,
            parse_strategy=parse_strategy,
            degraded=True,
            **common,
        )
        return CognitionResult(record=record, output=None)

    # ------------------------------------------------------------------ #
    # 直连边车的辅助操作（测试与运维用）
    # ------------------------------------------------------------------ #

    def inspect(self) -> Dict[str, Any]:
        """读取边车的自我描述（零工具 / 最小环境 / 已加载模块）。不调用模型。"""
        node = self.preflight()
        job = {
            "protocol": PROTOCOL,
            "jobId": f"cog-inspect-{uuid.uuid4().hex[:8]}",
            "operation": "inspect",
            "hostEnvAllowlist": list(self.config.host_env_allowlist),
        }
        outcome = self._invoke(job, node)
        return dict((outcome.frame or {})["result"])

    def parse_via_sidecar(self, text: str) -> Dict[str, Any]:
        """用**边车实现**的 JSON 抽取器处理文本（用于对照两套实现一致性）。"""
        node = self.preflight()
        job = {
            "protocol": PROTOCOL,
            "jobId": f"cog-parse-{uuid.uuid4().hex[:8]}",
            "operation": "parse",
            "text": text,
        }
        outcome = self._invoke(job, node)
        return dict((outcome.frame or {})["result"])

    def raw_outcome(self, job: Mapping[str, Any]) -> "_Outcome":
        """发送任意 job（测试注入故障用），返回原始往返结果。"""
        return self._invoke(dict(job), self.preflight())


@dataclass(frozen=True)
class _Outcome:
    """一次边车往返的原始观测（帧 + 噪声 + 耗时 + 退出码）。"""

    frame: Optional[Mapping[str, Any]]
    elapsed_ms: int
    return_code: int
    noise: Tuple[str, ...] = ()
    diagnostics: str = ""
    watchdog_timeout: bool = False


def _usage_from(payload: Optional[Mapping[str, Any]]) -> CognitionUsage:
    if not payload:
        return CognitionUsage()
    cost = payload.get("cost") or {}
    total = cost.get("total")
    return CognitionUsage(
        input_tokens=int(payload.get("input") or 0),
        output_tokens=int(payload.get("output") or 0),
        reasoning_tokens=(
            int(payload["reasoning"]) if payload.get("reasoning") is not None else None
        ),
        cache_read_tokens=int(payload.get("cacheRead") or 0),
        cache_write_tokens=int(payload.get("cacheWrite") or 0),
        total_tokens=int(payload.get("totalTokens") or 0),
        cost_total=(float(total) if isinstance(total, (int, float)) and total else None),
        cost_currency=("USD" if isinstance(total, (int, float)) and total else ""),
    )


def _looks_deprecated(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _DEPRECATED_MARKERS)


def _detail(exc: Optional[BaseException], dropped_chars: int) -> str:
    parts: List[str] = []
    if exc is not None:
        parts.append(f"{type(exc).__name__}: {exc}")
    if dropped_chars:
        parts.append(f"输入被截断：丢弃 {dropped_chars} 字符")
    return "; ".join(parts) or "ok"


def _detail_detail(detail: str, dropped_chars: int) -> str:
    if dropped_chars:
        return f"{detail}; 输入被截断：丢弃 {dropped_chars} 字符"
    return detail
