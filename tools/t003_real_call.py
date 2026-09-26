"""T-003 真实调用证据（硬规则 1）：Python → 边车（node + pi-ai）→ 远端模型 → 结构化结果。

跑法（WSL 内，需要代理）：

    export HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897
    ./.venv-new/bin/python tools/t003_real_call.py

输出包含：node 路径与版本、两条路由的配置（**不含密钥**）、真实耗时 / token /
成本、解析策略与解析结果。密钥只用于调用，绝不打印——
`CognitionConfig.api_key` 是 `repr=False`，记录里的 `credential_route` 只带指纹。

`--route deepseek|openai-compatible` 只跑一条；`--json` 输出机器可读结果。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlas.cognition import (  # noqa: E402
    DS_ROUTE,
    OA_ROUTE,
    CognitionConfig,
    CognitionRequest,
    PiSidecarCognitionPort,
    available_routes,
)

DOCUMENT = (
    "TSMC said it will expand advanced packaging capacity at its Chiayi facility next "
    "year, citing strong demand from AI accelerator customers. The company also flagged "
    "higher electricity costs in Taiwan as a margin risk, and said it is evaluating "
    "additional gas-fired power contracts."
)

CANDIDATES = ("ai", "semis", "energy")


def run_route(route: str) -> Dict[str, Any]:
    config = CognitionConfig.from_env(route=route)
    port = PiSidecarCognitionPort(config)
    node = port.preflight()

    started = time.monotonic()
    result = port.extract(
        CognitionRequest(
            raw_id=f"raw_t003_evidence_{route}",
            external_content=DOCUMENT,
            candidate_labels=CANDIDATES,
            kind="industry",
        )
    )
    wall_ms = int((time.monotonic() - started) * 1000)
    record = result.record

    return {
        "route": route,
        "node": node.describe(),
        "provider": config.provider,
        "base_url": config.base_url,
        "model": config.model,
        "credential_route": config.credential_route(),
        "api_key_env": config.api_key_env,
        "status": result.status.value,
        "reason": result.reason.value if result.reason else None,
        "detail": record.detail,
        "response_model": record.response_model,
        "elapsed_ms": record.elapsed_ms,
        "wall_ms": wall_ms,
        "usage": record.usage.as_dict(),
        "thinking_chars": record.thinking_chars,
        "parse_strategy": record.parse_strategy,
        "sidecar_code_version": record.sidecar_code_version,
        "code_version": record.code_version,
        "config_version": record.config_version,
        "model_version": record.model_version,
        "idempotency_key": record.idempotency_key,
        "tools_declared": record.tools_declared,
        "tool_calls": record.tool_calls,
        "claims": [claim.model_dump() for claim in result.claims],
        "quote_verbatim_hits": [
            claim.quote in DOCUMENT for claim in result.claims
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", default=None, choices=[DS_ROUTE, OA_ROUTE])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    routes: List[str] = [args.route] if args.route else [DS_ROUTE, OA_ROUTE]
    table = available_routes()
    reports: List[Dict[str, Any]] = []
    failures: List[str] = []

    for route in routes:
        if not table.get(route, {}).get("credential_available"):
            failures.append(f"{route}: 凭据不可用（环境变量未设置），跳过")
            continue
        try:
            reports.append(run_route(route))
        except Exception as exc:  # 响亮失败也要出现在证据里，不吞
            failures.append(f"{route}: {type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps({"reports": reports, "failures": failures}, ensure_ascii=False, indent=2))
        return 0 if reports and not failures else 1

    print("=" * 78)
    print("T-003 真实调用证据")
    print("=" * 78)
    print("\n路由可用性（不含密钥）：")
    print(json.dumps(table, ensure_ascii=False, indent=2))

    for report in reports:
        usage = report["usage"]
        cost = (
            "provider 未报告"
            if usage["cost_total"] is None
            else f'{usage["cost_total"]} {usage["cost_currency"]}'
        )
        print("\n" + "-" * 78)
        print(f'路由           : {report["route"]}')
        print(f'node           : {report["node"]}')
        print(f'provider/model : {report["provider"]} / {report["model"]}')
        print(f'base_url       : {report["base_url"]}')
        print(f'凭据路由标识   : {report["credential_route"]}   (key env={report["api_key_env"]})')
        print(f'响应 model     : {report["response_model"] or "(未报告)"}')
        print(f'状态           : {report["status"]}  reason={report["reason"]}')
        print(f'耗时           : 边车内 {report["elapsed_ms"]} ms / 端到端 {report["wall_ms"]} ms')
        print(
            "token          : "
            f'input={usage["input_tokens"]} output={usage["output_tokens"]} '
            f'reasoning={usage["reasoning_tokens"]} '
            f'cache_read={usage["cache_read_tokens"]} total={usage["total_tokens"]}'
        )
        print(f'成本           : {cost}')
        print(f'思考字符数     : {report["thinking_chars"]}')
        print(f'解析策略       : {report["parse_strategy"]}')
        print(f'零工具         : tools_declared={report["tools_declared"]} tool_calls={report["tool_calls"]}')
        print(f'版本三元组     : code={report["code_version"]} config={report["config_version"]} '
              f'model={report["model_version"]}')
        print(f'边车源码摘要   : {report["sidecar_code_version"]}')
        print(f'幂等键         : {report["idempotency_key"]}')
        print("claims         :")
        for claim, hit in zip(report["claims"], report["quote_verbatim_hits"]):
            print(f'   {claim}  quote_in_document={hit}')

    if failures:
        print("\n未完成的路由：")
        for failure in failures:
            print(f"  · {failure}")
    return 0 if reports and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
