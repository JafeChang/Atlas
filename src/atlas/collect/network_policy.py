"""跨进程共享的 robots 缓存与站点请求间隔。状态是可重建的运行缓存。"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import tempfile
import time
import urllib.robotparser
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from atlas.collect.fetch import FailureKind, FetchError
from atlas.collect.robots import RobotsFetchResult
from atlas.collect.throttle import ThrottleDecision, ThrottleTimeout, domain_of

ROBOTS_TTL = 24 * 3600


class SharedNetworkPolicy:
    """以站点为单位锁定实际请求，并持久保存 robots 与最近请求时间。"""

    def __init__(self, root, *, clock=time.time, sleeper=time.sleep, max_wait=30):
        self.root = Path(root) / "network-policy"
        self.clock, self.sleeper, self.max_wait = clock, sleeper, max_wait

    def _path(self, domain, suffix):
        return self.root / (hashlib.sha256(domain.encode()).hexdigest() + suffix)

    @contextmanager
    def _lock(self, domain, kind, mode=fcntl.LOCK_EX):
        self.root.mkdir(parents=True, exist_ok=True)
        with self._path(domain, "." + kind + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ThrottleTimeout(
                    domain, min_interval=0, needed=1, max_wait=0
                ) from error
            yield

    def _read(self, domain):
        path = self._path(domain, ".json")
        return json.loads(path.read_text()) if path.exists() else {}

    def _write(self, domain, state):
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".network-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self._path(domain, ".json"))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _delay(self, state):
        delays = [item.get("delay", 0) for item in state.get("robots", {}).values()]
        delays.extend(
            item["seconds"]
            for item in state.get("rates", {}).values()
            if self.clock() - item["updated_at"] < max(ROBOTS_TTL, item["seconds"] * 2)
        )
        return max(delays, default=0)

    def set_rate(self, url, channel_id, seconds):
        domain = domain_of(url)
        with self._lock(domain, "request"):
            state = self._read(domain)
            state.setdefault("rates", {})[channel_id] = {
                "seconds": seconds,
                "updated_at": self.clock(),
            }
            self._write(domain, state)

    def ready_at(self, url):
        domain = domain_of(url)
        # 原子替换使无锁读取也只看到完整快照；真正发请求仍须持锁检查。
        state = self._read(domain)
        return max(
            state.get("last_request", 0) + self._delay(state),
            state.get("deferred_until", 0),
        )

    def retry_snapshot(self, url):
        state = self._read(domain_of(url))
        until = state.get("deferred_until")
        if until is None:
            return None
        return {"until": until, "ready": self.clock() >= until}

    def defer_origin(self, origin, target, until):
        if domain_of(origin) == domain_of(target):
            return
        domain = domain_of(origin)
        with self._lock(domain, "request"):
            state = self._read(domain)
            state["deferred_until"] = max(state.get("deferred_until", 0), until)
            self._write(domain, state)

    @contextmanager
    def slot(self, url, interval=1.0, *, robots_request=False, domain=None):
        domain = domain or domain_of(url)
        # 刷新 robots 时阻止同站点的正文请求，直至新规则被保存。
        with (
            self._lock(domain, "robots", fcntl.LOCK_SH)
            if not robots_request
            else _null()
        ):
            with self._lock(domain, "request"):
                state = self._read(domain)
                delay = max(interval, self._delay(state))
                now = self.clock()
                needed = (
                    max(0.0, state.get("last_request", 0) + delay - now)
                    if state.get("last_request") is not None
                    else 0.0
                )
                if needed > self.max_wait:
                    state["deferred_until"] = state["last_request"] + delay
                    self._write(domain, state)
                    raise ThrottleTimeout(
                        domain,
                        min_interval=delay,
                        needed=needed,
                        max_wait=self.max_wait,
                    )
                if needed:
                    self.sleeper(needed)
                state["last_request"] = self.clock()
                self._write(domain, state)
                yield ThrottleDecision(
                    domain=domain, min_interval=delay, waited_seconds=needed
                )

    def robots(self, url, user_agent, fetch):
        domain = domain_of(url)
        parsed = urlsplit(url)
        key = json.dumps([parsed.scheme.lower(), parsed.netloc.lower(), user_agent])
        with self._lock(domain, "robots"):
            state = self._read(domain)
            cached = state.get("robots", {}).get(key)
            if cached and self.clock() - cached["fetched_at"] < ROBOTS_TTL:
                return RobotsFetchResult(
                    url=cached["url"],
                    status_code=cached["status"],
                    body=base64.b64decode(cached["body"]),
                )
            result = fetch(url, user_agent=user_agent)
            if (
                result.status_code is None
                or result.status_code >= 500
                or result.status_code == 429
            ):
                return result
            delay = 0
            if 200 <= result.status_code < 300:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(result.body.decode("utf-8", errors="replace").splitlines())
                delay = parser.crawl_delay(user_agent) or parser.crawl_delay("*") or 0
            with self._lock(domain, "request"):
                state = self._read(domain)
                state.setdefault("robots", {})[key] = {
                    "url": result.url,
                    "status": result.status_code,
                    "body": base64.b64encode(result.body).decode("ascii"),
                    "fetched_at": self.clock(),
                    "delay": delay,
                }
                self._write(domain, state)
            return result


@contextmanager
def _null():
    yield


class _PolicyWait(FetchError):
    kind = FailureKind.RATE_LIMIT_WAIT_TIMEOUT


class _PolicyDenied(FetchError):
    kind = FailureKind.ROBOTS_DENIED


class PolicyFetcher:
    """逐跳验证 robots 并占用对应站点请求槽，禁止 urllib 自动跳转。"""

    manages_policy = True

    def __init__(self, fetcher, robots, policy):
        self.fetcher, self.robots, self.policy = fetcher, robots, policy
        self.waited_seconds = 0.0

    def __call__(self, request):
        self.waited_seconds = 0.0
        url = request.url
        for _ in range(6):
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise _PolicyDenied("重定向目标不是有效 HTTP URL", url=url)
            if request.channel_id:
                try:
                    self.policy.set_rate(
                        url, request.channel_id, request.min_interval_seconds
                    )
                except ThrottleTimeout as error:
                    self.policy.defer_origin(
                        request.url, url, self.policy.clock() + error.needed
                    )
                    raise _PolicyWait(str(error), url=url) from error
            decision = self.robots.check(url, user_agent=request.user_agent)
            if not decision.allowed:
                if "ThrottleTimeout" in decision.reason:
                    self.policy.defer_origin(
                        request.url,
                        url,
                        max(self.policy.ready_at(url), self.policy.clock() + 1),
                    )
                    raise _PolicyWait(decision.reason, url=url)
                raise _PolicyDenied(decision.reason, url=url)
            try:
                with self.policy.slot(
                    url, max(request.min_interval_seconds, decision.crawl_delay)
                ) as slot:
                    self.waited_seconds += slot.waited_seconds
                    result = self.fetcher(replace(request, url=url))
            except ThrottleTimeout as error:
                self.policy.defer_origin(
                    request.url, url, self.policy.clock() + error.needed
                )
                raise _PolicyWait(str(error), url=url) from error
            if result.status_code not in (301, 302, 303, 307, 308):
                return result
            location = result.headers.get("location", "")
            if not location:
                raise _PolicyDenied("重定向缺少目标", url=url)
            url = urljoin(url, location)
        raise _PolicyDenied("重定向次数过多", url=url)
