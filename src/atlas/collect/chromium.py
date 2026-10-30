"""显式登记的来源使用真实 Chromium；robots/限速/重定向仍由调用方执行。"""

from __future__ import annotations

import base64
import json
import os
import select
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

from .fetch import FetchRequest, FetchResult, FetchTimeout, FetchTransportError

BROWSER_TAG = "chromium"


def browser_routes(root):
    """只读取显式标记的渠道；没有注册表的旧库不启用浏览器传输。"""
    path = Path(root) / "atlas.db"
    if not path.is_file():
        return {}
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='channels' AND type='table'"
        ).fetchone():
            return {}
        routes = {}
        for (payload,) in db.execute("SELECT payload FROM channels"):
            channel = json.loads(payload)
            if BROWSER_TAG not in channel.get("tags", ()):
                continue
            agent = channel.get("user_agent")
            if not agent or not channel.get("user_agent_justification"):
                raise ValueError("Chromium 渠道必须登记实际浏览器身份和用途说明")
            host = urlsplit(channel["endpoint"]).hostname
            if host in routes and routes[host] != agent:
                raise ValueError("同站点的 Chromium 身份配置冲突")
            routes[host] = agent
        return routes


class ChromiumFetcher:
    """独立桌面浏览器读取原始响应；不执行网页 JS、不处理挑战或登录。"""

    def __init__(self, *, executable=None):
        self.executable = executable or os.environ.get("ATLAS_CHROMIUM_EXECUTABLE")

    @contextmanager
    def _page(self, url, timeout):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FetchTransportError(
                "Chromium 传输需要 uv sync --extra browser", url=url
            ) from exc
        if self.executable and not Path(self.executable).is_file():
            raise FetchTransportError("ATLAS_CHROMIUM_EXECUTABLE 不存在", url=url)
        xvfb_binary = shutil.which("Xvfb")
        if not xvfb_binary:
            raise FetchTransportError("Chromium 桌面读取需要 Xvfb", url=url)
        proxy = None
        parsed = urlsplit(url)
        if parsed.hostname and not proxy_bypass(parsed.hostname):
            address = getproxies().get(parsed.scheme)
            if address:
                parts = urlsplit(address if "://" in address else "http://" + address)
                proxy = {
                    "server": f"{parts.scheme}://{parts.hostname}:{parts.port or 80}"
                }
                if parts.username:
                    proxy.update(
                        username=unquote(parts.username),
                        password=unquote(parts.password or ""),
                    )
        display = subprocess.Popen(
            [
                xvfb_binary,
                "-displayfd",
                "1",
                "-screen",
                "0",
                "1280x1024x24",
                "-nolisten",
                "tcp",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            if not select.select([display.stdout], [], [], min(5, timeout))[0]:
                raise FetchTimeout("Xvfb 启动超时", url=url)
            number = display.stdout.readline().strip()
            if not number.isdigit():
                raise FetchTransportError("Xvfb 未能启动", url=url)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=self.executable,
                    headless=False,
                    env={**os.environ, "DISPLAY": ":" + number},
                    proxy=proxy,
                    timeout=timeout * 1000,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    # RSS/robots 无须脚本；不执行反爬挑战或第三方资源。
                    context = browser.new_context(
                        java_script_enabled=False, service_workers="block"
                    )
                    yield context.new_page()
                finally:
                    browser.close()
        finally:
            display.terminate()
            try:
                display.wait(timeout=5)
            except subprocess.TimeoutExpired:
                display.kill()
                display.wait()
            display.stdout.close()

    def user_agent(self):
        with self._page("about:blank", 15) as page:
            return page.evaluate("navigator.userAgent")

    def __call__(self, request: FetchRequest):
        from playwright.sync_api import Error
        from playwright.sync_api import TimeoutError as BrowserTimeout

        if request.method != "GET":
            raise FetchTransportError("Chromium 传输仅支持 GET", url=request.url)
        parsed = urlsplit(request.url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise FetchTransportError(
                "Chromium 请求必须是无凭据的 HTTP(S) URL", url=request.url
            )
        started = time.monotonic()
        try:
            with self._page(request.url, request.timeout_seconds) as page:
                actual_agent = page.evaluate("navigator.userAgent")
                if actual_agent != request.user_agent:
                    raise FetchTransportError(
                        "Chromium 版本/身份已变化，请重新配置并验证信源",
                        url=request.url,
                    )
                captured, failures, requested = [], [], []
                session = page.context.new_cdp_session(page)
                session.send(
                    "Fetch.enable",
                    {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                )

                def route_request(event):
                    request_id = event["requestId"]
                    try:
                        if "responseStatusCode" in event:
                            status = event["responseStatusCode"]
                            headers = {
                                item["name"].lower(): item["value"]
                                for item in event.get("responseHeaders", [])
                            }
                            body = b""
                            if status not in (301, 302, 303, 307, 308, 204, 304):
                                payload = session.send(
                                    "Fetch.getResponseBody", {"requestId": request_id}
                                )
                                body = (
                                    base64.b64decode(payload["body"])
                                    if payload["base64Encoded"]
                                    else payload["body"].encode("utf-8")
                                )
                            captured.append(
                                FetchResult(
                                    url=event["request"]["url"],
                                    status_code=status,
                                    content=body,
                                    headers=headers,
                                    elapsed_seconds=time.monotonic() - started,
                                )
                            )
                            # 原响应按原样返回调用方；浏览器内终止导航，避免渲染、图标或跳转请求。
                            session.send(
                                "Fetch.fulfillRequest",
                                {
                                    "requestId": request_id,
                                    "responseCode": 204,
                                    "body": "",
                                },
                            )
                        elif event["resourceType"] == "Document" and not requested:
                            requested.append(event["request"]["url"])
                            session.send(
                                "Fetch.continueRequest",
                                {"requestId": request_id, "interceptResponse": True},
                            )
                        else:
                            session.send(
                                "Fetch.failRequest",
                                {"requestId": request_id, "errorReason": "Aborted"},
                            )
                    except Error as error:
                        failures.append(error)
                        session.send(
                            "Fetch.failRequest",
                            {"requestId": request_id, "errorReason": "Aborted"},
                        )

                session.on("Fetch.requestPaused", route_request)
                try:
                    try:
                        page.goto(
                            request.url,
                            wait_until="domcontentloaded",
                            timeout=request.timeout_seconds * 1000,
                        )
                    except Error as error:
                        if not captured or "ERR_ABORTED" not in str(error):
                            raise
                    if failures:
                        raise FetchTransportError(
                            "浏览器响应读取失败：" + str(failures[0]), url=request.url
                        )
                    if not captured:
                        raise FetchTransportError(
                            "浏览器没有返回 HTTP 响应", url=request.url
                        )
                    if len(captured[0].content) > 32 * 1024 * 1024:
                        raise FetchTransportError(
                            "浏览器响应超过 32 MiB", url=request.url
                        )
                    return captured[0]
                finally:
                    session.remove_listener("Fetch.requestPaused", route_request)
                    page.close()
        except BrowserTimeout as exc:
            raise FetchTimeout("Chromium 请求超时", url=request.url) from exc
        except Error as exc:
            raise FetchTransportError(
                "Chromium 请求失败：" + str(exc), url=request.url
            ) from exc


class BrowserRoutedFetcher:
    def __init__(self, fallback, routes, *, browser=None):
        self.fallback, self.routes = fallback, routes
        self.browser = browser or ChromiumFetcher()

    def __call__(self, request):
        expected = self.routes.get(urlsplit(request.url).hostname)
        if expected is None:
            if request.user_agent in self.routes.values():
                # 浏览器请求跳到其他站点时也保持真实客户端身份。
                return self.browser(request)
            return self.fallback(request)
        if request.user_agent != expected:
            raise FetchTransportError(
                "浏览器来源的请求身份与登记配置不一致", url=request.url
            )
        return self.browser(request)
