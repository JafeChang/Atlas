"""T-109 打标前端的验收测试：真实服务、本地临时端口、**不出网**。

覆盖任务书判据（**全部端到端**，不用假对象冒充服务）：

1. **端到端打标闭环**：真归档 + 真 feed 查询层 + 真 HTTP 服务 + 真 Confirmed 库：
   `GET /feed` 拉列表 → `POST /label` 打标 → **独立打开同一个 `LabelStore`** 断言已落库
   → 再次 `GET /feed` 页面能看到该标签；
2. **幂等**：同一判断重复 POST，库里只有一条（并断言库文件字节不变）；
3. **只读性**：浏览 / 筛选 / 翻页路径不产生任何写入 —— 库文件 sha256 + `count()` 前后一致，
   且注入一个「`record()` 一被调用就抛错」的探针，证明浏览路径**根本没有尝试**写入；
4. **非法输入被拒绝**：未知 `raw_id`、非法标签值、空参数、重复字段、未知字段、
   错误 Content-Type / 缺失 Content-Length / 超大请求体 → 明确 4xx + 可读信息，绝不静默成功；
5. **不硬编码行业**：静态扫描 webui 源码不含行业名字面量；并且换一个注入的 provider，
   页面选项与校验集合都跟着换；
6. **只绑 `127.0.0.1:0`**，用 `ThreadingHTTPServer`，测完必须关闭并释放端口。

只读性为什么这样证明：`StoreLabelAccess.read_session()` 只用 `LabelStore` 的读方法
（`keys_for` / `latest_value`），写方法 `add` 只在 `record()` 里被调用一次；
另有 AST 静态检查钉死「全包只有一处 `.add(` 调用，且在 `StoreLabelAccess.record` 内」。
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple

import pytest

from atlas.archive import open_archive
from atlas.contracts import ConfirmedLabel, RawRecord
from atlas.contracts.ids import content_sha256
from atlas.feed import StaticFeedSource
from atlas.feed import archive_source_factory
from atlas.labels import open_store
from atlas.registry import (
    Channel,
    FetchSpec,
    Industry,
    RegistryService,
    SqliteConfigStore,
)
from atlas.webui import (
    LABEL_KEY_INDUSTRY,
    LABEL_KEY_VALID,
    LABEL_VALUE_INVALID,
    LABEL_VALUE_VALID,
    MAX_BODY_BYTES,
    StoreLabelAccess,
    WebUIApplication,
    build_application,
    raw_exists_from_source,
)

BASE = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
WEBUI_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "atlas" / "webui"
WEBUI_MODULES = sorted(WEBUI_PACKAGE.glob("*.py"))

#: 测试用的**行业配置**。它们只出现在测试与注入的 provider 里；
#: `src/atlas/webui/` 内不得出现这些字面量（判据 5）。
TEST_INDUSTRIES = ("cs.LG", "stat.ML", "q-bio.NC")
INDUSTRY_MAP = {"chan-0": TEST_INDUSTRIES[0], "chan-1": TEST_INDUSTRIES[1]}

#: `atlas.registry` 的 `Industry.id` 只允许小写字母/数字/单个短横线（`validate_id`），
#: 因此接真实配置的用例用这一组 id。
REGISTRY_INDUSTRIES = ("arxiv-cs-lg", "arxiv-stat-ml", "q-bio-nc")

#: 归档里的三份原文（chan-2 故意没有行业归属，验证 `None` 的展示）。
DEMO_RECORDS = (
    ("arch-0", "chan-0", BASE, b"payload-0"),
    ("arch-1", "chan-1", BASE + timedelta(minutes=1), b"payload-1"),
    ("arch-2", "chan-2", BASE + timedelta(minutes=2), b"payload-2"),
)


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #
def make_record(
    raw_id: str, channel_id: str, fetched_at: datetime, payload: bytes
) -> RawRecord:
    return RawRecord(
        raw_id=raw_id,
        channel_id=channel_id,
        endpoint=f"https://example.test/{raw_id}",
        content_sha256=content_sha256(payload),
        byte_length=len(payload),
        fetched_at=fetched_at,
        http_status=200,
    )


def seed_archive(root: Path) -> None:
    archive = open_archive(root)
    try:
        for raw_id, channel_id, fetched_at, payload in DEMO_RECORDS:
            archive.put(make_record(raw_id, channel_id, fetched_at, payload), payload)
        assert archive.verify() == []
    finally:
        archive.close()


def archive_has(root: Path) -> Callable[[str], bool]:
    """「文档存在」判定：直接来自归档（T-103），不依赖 webui 自己的推导。"""

    def exists(raw_id: str) -> bool:
        archive = open_archive(root)
        try:
            return raw_id in set(archive.all_raw_ids())
        finally:
            archive.close()

    return exists


def files_snapshot(root: Path) -> Dict[str, str]:
    """目录内容快照：相对路径 → sha256（**按字节比**，不看 mtime）。"""
    out: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def label_rows(db_path: Path) -> int:
    with open_store(db_path) as store:
        return store.count()


@contextmanager
def running_webui(
    tmp_path: Path,
    *,
    industries: Any = TEST_INDUSTRIES,
    industry_map: Optional[Dict[str, str]] = None,
    label_db: Optional[Path] = None,
    initialize: bool = True,
    access_factory: Optional[Callable[[Path], StoreLabelAccess]] = None,
    actor: str = "tester",
) -> Iterator[SimpleNamespace]:
    """起一个真实的 webui 服务（`127.0.0.1:0`），退出时必定关闭。

    `industries` 可以是行业序列，也可以是零参可调用对象（例如
    `atlas.registry.RegistryService.label_space`）——用来证明行业**只**来自注入的配置。
    """
    archive_root = tmp_path / "store"
    seed_archive(archive_root)
    db_path = Path(label_db) if label_db is not None else archive_root / "atlas.db"

    provider = industries if callable(industries) else (lambda: tuple(industries))
    factory = access_factory or (lambda path: StoreLabelAccess(path))
    labels = factory(db_path)
    if initialize:
        labels.initialize()

    app = WebUIApplication(
        archive_source_factory(
            archive_root, industry_of=INDUSTRY_MAP if industry_map is None else industry_map
        ),
        labels=labels,
        industry_provider=provider,
        raw_exists=archive_has(archive_root),
        actor=actor,
    )
    try:
        assert app.host == "127.0.0.1"
        assert app.port != 0
        yield SimpleNamespace(
            app=app,
            archive_root=archive_root,
            db_path=db_path,
            industries=tuple(provider()),
            actor=actor,
        )
    finally:
        app.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟随跳转：要断言 303 与 Location 的确切值。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _http(
    app: WebUIApplication,
    path: str,
    *,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    method: Optional[str] = None,
    follow: bool = False,
) -> Tuple[int, str, Dict[str, str]]:
    request = urllib.request.Request(
        app.base_url + path, data=data, headers=headers or {}, method=method
    )
    handlers = [] if follow else [_NoRedirect]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def get(app: WebUIApplication, path: str) -> Tuple[int, str, Dict[str, str]]:
    return _http(app, path)


def judgment(
    raw_id: str,
    label_value: str,
    *,
    label_key: str = LABEL_KEY_VALID,
    actor: str = "tester",
    return_to: str = "/feed",
) -> Dict[str, str]:
    """一次点击 = 一个判断 = 一条标签（文档级锚点，SPEC §2.1 / 1A）。"""
    return {
        "raw_id": raw_id,
        "label_key": label_key,
        "label_value": label_value,
        "actor": actor,
        "return_to": return_to,
    }


def submit(
    app: WebUIApplication,
    fields: Dict[str, str],
    *,
    follow: bool = False,
    content_type: str = "application/x-www-form-urlencoded",
) -> Tuple[int, str, Dict[str, str]]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return _http(app, "/label", data=body, headers={"Content-Type": content_type}, follow=follow)


def raw_post(
    app: WebUIApplication, headers: Dict[str, str], *, body: bytes = b""
) -> Tuple[int, str]:
    """底层 POST：可以故意不带 Content-Length 或声明一个超大的长度。"""
    conn = http.client.HTTPConnection(app.host, app.port, timeout=5)
    try:
        conn.putrequest("POST", "/label")
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if body:
            try:
                conn.send(body)
            except (BrokenPipeError, ConnectionResetError):  # 服务端已拒绝并关连接
                pass
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        conn.close()


def page_shows_label(page: str, label_key: str, label_value: str) -> bool:
    return f'<span class="label-tag">{label_key}={label_value}</span>' in page


# ---------------------------------------------------------------------- #
# 判据 1：端到端打标闭环
# ---------------------------------------------------------------------- #
def test_document_level_label_round_trip_end_to_end(tmp_path: Path) -> None:
    with running_webui(tmp_path) as world:
        app = world.app

        # -- 1) GET /feed 拉到列表 --------------------------------------
        status, page, _ = get(app, "/feed?order=asc")
        assert status == 200
        assert '<code class="raw-id">arch-0</code>' in page
        assert page.count('<p class="labels">未打标</p>') == len(DEMO_RECORDS)

        # -- 2) POST 打标（判据要求的第一个判断：有效） ------------------
        status, _, headers = submit(app, judgment("arch-0", LABEL_VALUE_VALID))
        assert status == 303, "打标成功应重定向回 feed（Post/Redirect/Get）"
        assert headers["Location"] == "/feed"

        # -- 3) 独立打开同一个 LabelStore 断言已落库 --------------------
        with open_store(world.db_path) as store:
            assert store.count() == 1
            assert store.latest_value("arch-0", LABEL_KEY_VALID) == LABEL_VALUE_VALID
            stored = store.all_for("arch-0")
            assert [
                (row.raw_id, row.label_key, row.label_value, row.actor) for row in stored
            ] == [("arch-0", LABEL_KEY_VALID, LABEL_VALUE_VALID, "tester")]

        # -- 4) 再次 GET 页面能看到该标签 -------------------------------
        status, page, _ = get(app, "/feed?order=asc")
        assert status == 200
        assert page_shows_label(page, LABEL_KEY_VALID, LABEL_VALUE_VALID), page

        # -- 5) 行业修正走同一条路（C8：取值来自配置，不来自代码） ------
        industry_value = TEST_INDUSTRIES[1]
        status, landed, _ = submit(
            app,
            judgment("arch-1", industry_value, label_key=LABEL_KEY_INDUSTRY),
            follow=True,
        )
        assert status == 200, "浏览器式跟随跳转后应落在 feed 页上"
        assert "Atlas Feed" in landed
        with open_store(world.db_path) as store:
            assert store.latest_value("arch-1", LABEL_KEY_INDUSTRY) == industry_value
            assert store.count() == 2

        status, page, _ = get(app, "/feed?order=asc")
        assert status == 200
        assert page_shows_label(page, LABEL_KEY_INDUSTRY, industry_value), page

        # -- 6) 打了标的文档能被 feed 的标签筛选到（C8 闭环） -----------
        status, filtered, _ = get(app, "/feed?labeled=true&order=asc")
        assert status == 200
        assert 'id="item-arch-0"' in filtered
        assert 'id="item-arch-1"' in filtered
        assert 'id="item-arch-2"' not in filtered

        status, unlabeled, _ = get(app, "/feed?labeled=false&order=asc")
        assert status == 200
        assert 'id="item-arch-2"' in unlabeled
        assert 'id="item-arch-0"' not in unlabeled


def test_industry_filter_uses_the_registry_backed_industry_map(tmp_path: Path) -> None:
    """行业筛选维度 = 渠道配置里的行业（SPEC §2.5 的四个引用之一）。"""
    with running_webui(tmp_path) as world:
        status, page, _ = get(world.app, f"/feed?industry={TEST_INDUSTRIES[0]}&order=asc")
        assert status == 200
        assert 'id="item-arch-0"' in page
        assert 'id="item-arch-1"' not in page


# ---------------------------------------------------------------------- #
# 判据 2：幂等（只增不改 + 同判断不重复）
# ---------------------------------------------------------------------- #
def test_repeated_identical_judgment_is_idempotent(tmp_path: Path) -> None:
    with running_webui(tmp_path) as world:
        fields = judgment("arch-0", LABEL_VALUE_VALID)

        assert submit(world.app, fields)[0] == 303
        after_first = hashlib.sha256(world.db_path.read_bytes()).hexdigest()

        assert submit(world.app, fields)[0] == 303
        assert submit(world.app, fields)[0] == 303

        with open_store(world.db_path) as store:
            assert store.count() == 1, "同一判断重复提交不得产生第二条"
            assert len(store.all_for("arch-0")) == 1
        assert hashlib.sha256(world.db_path.read_bytes()).hexdigest() == after_first


def test_changed_judgment_appends_a_new_record(tmp_path: Path) -> None:
    """改判不是覆盖：Confirmed 只增不改，`latest_value` 取最新（SPEC §2.3）。"""
    with running_webui(tmp_path) as world:
        assert submit(world.app, judgment("arch-0", LABEL_VALUE_VALID))[0] == 303
        assert submit(world.app, judgment("arch-0", LABEL_VALUE_INVALID))[0] == 303

        with open_store(world.db_path) as store:
            assert store.count() == 2
            assert [row.label_value for row in store.all_for("arch-0")] == [
                LABEL_VALUE_VALID,
                LABEL_VALUE_INVALID,
            ]
            assert store.latest_value("arch-0", LABEL_KEY_VALID) == LABEL_VALUE_INVALID

        _, page, _ = get(world.app, "/feed?order=asc")
        assert page_shows_label(page, LABEL_KEY_VALID, LABEL_VALUE_INVALID)
        assert not page_shows_label(page, LABEL_KEY_VALID, LABEL_VALUE_VALID)


# ---------------------------------------------------------------------- #
# 判据 3：浏览路径只读
# ---------------------------------------------------------------------- #
class _WriteForbiddenAccess(StoreLabelAccess):
    """写就炸：用来证明浏览路径**根本没有尝试**过写入。"""

    def record(self, **kwargs: Any):  # noqa: ANN201 - 故意抛错
        raise AssertionError(f"只读浏览路径不得打标：{kwargs}")


def test_browsing_filtering_and_paging_never_write(tmp_path: Path) -> None:
    with running_webui(tmp_path, access_factory=_WriteForbiddenAccess) as world:
        # 先直接落一条标签（走 atlas.labels 的唯一写入口），让库里既有 schema 又有数据；
        # 之后所有浏览都经过「写就炸」的探针。
        with open_store(world.db_path) as store:
            store.add(
                ConfirmedLabel.human(
                    raw_id="arch-0",
                    label_key=LABEL_KEY_VALID,
                    label_value=LABEL_VALUE_VALID,
                    actor="tester",
                )
            )

        rows_before = label_rows(world.db_path)
        files_before = files_snapshot(world.archive_root)
        assert rows_before == 1

        browse_paths = [
            "/feed",
            "/feed?order=asc",
            "/feed?order=asc&limit=1",
            "/feed?order=asc&limit=1&offset=1",
            "/feed?order=asc&limit=2&offset=1",
            f"/feed?industry={TEST_INDUSTRIES[0]}",
            "/feed?labeled=true",
            "/feed?labeled=false",
            f"/feed?labels={LABEL_KEY_VALID}",
            "/feed?channel=chan-0",
            "/health",
            "/feed?limit=0",
            "/feed?nonsense=1",
            "/nope",
            "/label",
        ]
        statuses = [get(world.app, path)[0] for path in browse_paths]
        assert statuses == [
            200, 200, 200, 200, 200, 200, 200, 200, 200, 200,  # 只读浏览
            200,                                                # /health
            400, 400,                                           # 非法查询参数
            404,                                                # 未知路径
            405,                                                # GET /label
        ]

        assert files_snapshot(world.archive_root) == files_before, "浏览不得改动归档或标签库"
        assert label_rows(world.db_path) == rows_before == 1


def test_browsing_does_not_create_a_label_store(tmp_path: Path) -> None:
    """库还不存在时，纯浏览**不创建**库文件、不建目录（`initialize()` 归应用启动）。"""
    label_db = tmp_path / "labels" / "atlas.db"
    with running_webui(tmp_path, label_db=label_db, initialize=False) as world:
        assert not label_db.exists()

        assert get(world.app, "/feed")[0] == 200
        assert get(world.app, "/feed?labeled=true")[0] == 200
        assert not label_db.exists()
        assert not label_db.parent.exists()

        # 只有真正打标才落盘
        assert submit(world.app, judgment("arch-0", LABEL_VALUE_VALID))[0] == 303
        assert label_db.exists()
        assert label_rows(label_db) == 1


# ---------------------------------------------------------------------- #
# 判据 4：非法输入被拒绝
# ---------------------------------------------------------------------- #
def test_invalid_submissions_are_rejected_with_readable_errors(tmp_path: Path) -> None:
    with running_webui(tmp_path) as world:
        cases = [
            (judgment("does-not-exist", LABEL_VALUE_VALID), 404, "不在归档里"),
            (judgment("arch-0", "maybe"), 400, "只接受"),
            (
                judgment("arch-0", "astrology", label_key=LABEL_KEY_INDUSTRY),
                400,
                "不在当前配置的行业列表里",
            ),
            ({**judgment("arch-0", LABEL_VALUE_VALID), "label_key": "topic"}, 400, "未知 label_key"),
            ({**judgment("arch-0", LABEL_VALUE_VALID), "raw_id": "   "}, 400, "不得为空"),
            ({**judgment("arch-0", LABEL_VALUE_VALID), "actor": ""}, 400, "不得为空"),
            ({**judgment("arch-0", LABEL_VALUE_VALID), "label_value": ""}, 400, "不得为空"),
            (
                {"label_key": LABEL_KEY_VALID, "label_value": LABEL_VALUE_VALID, "actor": "tester"},
                400,
                "缺少必填字段",
            ),
            ({**judgment("arch-0", LABEL_VALUE_VALID), "extra": "1"}, 400, "未知表单字段"),
            (
                {**judgment("arch-0", LABEL_VALUE_VALID), "return_to": "https://evil.test/"},
                400,
                "return_to 非法",
            ),
            (
                {**judgment("arch-0", LABEL_VALUE_VALID), "return_to": "/feed?evil=1"},
                400,
                "return_to 非法",
            ),
        ]
        for fields, expected_status, needle in cases:
            status, page, _ = submit(world.app, fields)
            assert status == expected_status, (fields, status)
            assert needle in page, (fields, page)

        # 重复字段（同名出现两次）→ 语义不明，拒绝
        duplicated = urllib.parse.urlencode(
            [
                ("raw_id", "arch-0"),
                ("raw_id", "arch-1"),
                ("label_key", LABEL_KEY_VALID),
                ("label_value", LABEL_VALUE_VALID),
                ("actor", "tester"),
            ]
        ).encode("utf-8")
        status, page, _ = _http(
            world.app,
            "/label",
            data=duplicated,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 400 and "重复提交" in page

        assert label_rows(world.db_path) == 0, "被拒绝的请求不得留下任何标签"


def test_malformed_http_bodies_are_rejected(tmp_path: Path) -> None:
    with running_webui(tmp_path) as world:
        # Content-Type 不对
        status, page, _ = submit(
            world.app, judgment("arch-0", LABEL_VALUE_VALID), content_type="application/json"
        )
        assert status == 415
        assert "application/x-www-form-urlencoded" in page

        # 空请求体
        status, page, _ = _http(
            world.app,
            "/label",
            data=b"",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 400 and "请求体为空" in page

        # 缺 Content-Length
        status, body = raw_post(world.app, {"Content-Type": "application/x-www-form-urlencoded"})
        assert status == 411 and "Content-Length" in body

        # 声明一个超过上限的长度（头部就能判定，不必读完）
        status, body = raw_post(
            world.app,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(MAX_BODY_BYTES + 1),
            },
            body=b"x" * 16,
        )
        assert status == 413 and "过大" in body

        assert label_rows(world.db_path) == 0


def test_method_and_path_rejections(tmp_path: Path) -> None:
    with running_webui(tmp_path) as world:
        status, page, headers = get(world.app, "/label")
        assert status == 405 and "POST" in headers["Allow"]

        status, _, headers = _http(
            world.app,
            "/feed",
            data=b"x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        assert status == 405 and headers["Allow"] == "GET"

        status, _, headers = _http(world.app, "/feed", method="PUT")
        assert status == 405 and headers["Allow"] == "GET, POST"

        status, page, _ = get(world.app, "/nope")
        assert status == 404 and "未知路径" in page


def test_health_endpoint_is_json(tmp_path: Path) -> None:
    with running_webui(tmp_path) as world:
        status, body, headers = get(world.app, "/health")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body)["status"] == "ok"


# ---------------------------------------------------------------------- #
# 判据 5：不硬编码行业枚举
# ---------------------------------------------------------------------- #
def test_webui_sources_contain_no_industry_name_literals() -> None:
    literals = TEST_INDUSTRIES + REGISTRY_INDUSTRIES
    for path in WEBUI_MODULES:
        text = path.read_text(encoding="utf-8")
        for industry in literals:
            assert industry not in text, f"{path.name} 里出现硬编码行业名 {industry!r}"


def test_industry_options_and_validation_follow_the_injected_provider(tmp_path: Path) -> None:
    first = (TEST_INDUSTRIES[0],)
    second = (TEST_INDUSTRIES[1], TEST_INDUSTRIES[2])

    with running_webui(tmp_path, industries=first, industry_map={}) as world:
        status, page, _ = get(world.app, "/feed")
        assert status == 200
        assert f'<option value="{first[0]}">{first[0]}</option>' in page
        for other in second:
            assert other not in page, "行业选项只能来自注入的配置"

        # 校验集合同样跟着配置走：配置外的行业被拒绝
        status, page, _ = submit(
            world.app,
            judgment("arch-0", TEST_INDUSTRIES[1], label_key=LABEL_KEY_INDUSTRY),
        )
        assert status == 400 and "不在当前配置的行业列表里" in page

        status, _, _ = submit(
            world.app, judgment("arch-0", first[0], label_key=LABEL_KEY_INDUSTRY)
        )
        assert status == 303

    # 换一份配置（另起一份干净的库，避免上一轮的标签干扰断言）
    with running_webui(tmp_path / "second", industries=second, industry_map={}) as world:
        status, page, _ = get(world.app, "/feed")
        assert status == 200
        for other in second:
            assert f'<option value="{other}">{other}</option>' in page
        assert first[0] not in page, "换一份配置后，旧行业不得再出现"


def test_empty_industry_configuration_is_rejected_loudly(tmp_path: Path) -> None:
    with running_webui(tmp_path, industries=()) as world:
        status, page, _ = submit(
            world.app, judgment("arch-0", TEST_INDUSTRIES[0], label_key=LABEL_KEY_INDUSTRY)
        )
        assert status == 400
        assert "没有任何行业" in page
        assert label_rows(world.db_path) == 0


def test_c8_loop_runs_against_the_real_registry_configuration(tmp_path: Path) -> None:
    """C8 闭环的真实接线：行业来自 `atlas.registry` 的配置。

    同一份配置同时决定 **feed 的筛选维度** 与 **打标的修正取值集合**
    （SPEC §2.5：前端配的行业 = AI 分类的标签空间 = feed 的筛选维度 = 打标时的修正对象）。
    停用的行业不在 `label_space()` 里，因此既不出现在页面上，也不可能被打成标签。
    """
    config = SqliteConfigStore(
        author="tester",
        industries=(
            Industry(id=REGISTRY_INDUSTRIES[0], name="arXiv cs.LG", enabled=True),
            Industry(id=REGISTRY_INDUSTRIES[1], name="arXiv stat.ML", enabled=True),
            Industry(id=REGISTRY_INDUSTRIES[2], name="已停用", enabled=False),
        ),
        channels=(
            Channel(
                id="chan-0",
                industry_id=REGISTRY_INDUSTRIES[0],
                type="rss",
                endpoint="https://example.test/a.xml",
                fetch_spec=FetchSpec(type="rss"),
                interval_seconds=3600,
                enabled=True,
            ),
            Channel(
                id="chan-1",
                industry_id=REGISTRY_INDUSTRIES[1],
                type="rss",
                endpoint="https://example.test/b.xml",
                fetch_spec=FetchSpec(type="rss"),
                interval_seconds=3600,
                enabled=True,
            ),
        ),
        db_path=tmp_path / "config" / "registry.db",
    )
    try:
        service = RegistryService(config)
        assert service.label_space() == (REGISTRY_INDUSTRIES[0], REGISTRY_INDUSTRIES[1])
        # 渠道 → 行业 也取自同一份配置（feed 的 industry_of 就是这么接的）
        industry_of = {item.id: item.industry_id for item in service.snapshot.channels}

        with running_webui(
            tmp_path, industries=service.label_space, industry_map=industry_of
        ) as world:
            status, page, _ = get(world.app, "/feed?order=asc")
            assert status == 200
            assert f'<option value="{REGISTRY_INDUSTRIES[0]}">{REGISTRY_INDUSTRIES[0]}</option>' in page
            assert REGISTRY_INDUSTRIES[2] not in page, "停用的行业不在标签空间里"

            # 筛选维度就是配置里的行业（渠道 chan-0 挂在 REGISTRY_INDUSTRIES[0] 下）
            status, filtered, _ = get(
                world.app, f"/feed?industry={REGISTRY_INDUSTRIES[0]}&order=asc"
            )
            assert status == 200
            assert 'id="item-arch-0"' in filtered
            assert 'id="item-arch-1"' not in filtered

            # 打标修正对象也来自同一份配置
            status, _, _ = submit(
                world.app,
                judgment("arch-0", REGISTRY_INDUSTRIES[1], label_key=LABEL_KEY_INDUSTRY),
            )
            assert status == 303
            with open_store(world.db_path) as store:
                assert store.latest_value("arch-0", LABEL_KEY_INDUSTRY) == REGISTRY_INDUSTRIES[1]

            # 停用行业即使被手工提交也拒绝
            status, page, _ = submit(
                world.app,
                judgment("arch-0", REGISTRY_INDUSTRIES[2], label_key=LABEL_KEY_INDUSTRY),
            )
            assert status == 400 and "不在当前配置的行业列表里" in page
    finally:
        config.close()


# ---------------------------------------------------------------------- #
# 判据 6：只绑环回、用 ThreadingHTTPServer、测完关闭
# ---------------------------------------------------------------------- #
def test_server_binds_loopback_only_and_releases_the_port(tmp_path: Path) -> None:
    with running_webui(tmp_path) as world:
        assert world.app.host == "127.0.0.1"
        assert world.app.port != 0
        assert isinstance(world.app.server, ThreadingHTTPServer)
        assert world.app.server.daemon_threads is True
        base_url = world.app.base_url

    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(base_url + "/health", timeout=2)


# ---------------------------------------------------------------------- #
# 接线：默认 seam 与静态边界
# ---------------------------------------------------------------------- #
def test_store_label_access_requires_an_explicit_db_path() -> None:
    """本包不提供任何指向仓库 `data/` 的默认库路径。"""
    with pytest.raises(TypeError):
        StoreLabelAccess()  # type: ignore[call-arg]


def test_build_application_wires_static_source_end_to_end(tmp_path: Path) -> None:
    """便捷装配路径：`build_application` + `StaticFeedSource`（含 `raw_exists` 推导）。"""
    records = [
        make_record(raw_id, channel_id, fetched_at, payload)
        for raw_id, channel_id, fetched_at, payload in DEMO_RECORDS
    ]
    source = StaticFeedSource(records, industry_of=dict(INDUSTRY_MAP))
    db_path = tmp_path / "labels" / "atlas.db"

    app = build_application(
        source, db_path=db_path, industry_provider=lambda: tuple(TEST_INDUSTRIES)
    )
    try:
        assert app.labels.db_path == db_path and db_path.exists()
        status, page, _ = get(app, "/feed?order=asc")
        assert status == 200 and '<code class="raw-id">arch-0</code>' in page

        assert submit(app, judgment("arch-0", LABEL_VALUE_VALID))[0] == 303
        assert label_rows(db_path) == 1

        assert submit(app, judgment("ghost", LABEL_VALUE_VALID))[0] == 404
        assert label_rows(db_path) == 1
    finally:
        app.close()


class _PagedSource:
    """没有 `by_id` 的 `FeedSource`：验证 `raw_exists_from_source` 的分页回退路径。"""

    def __init__(self, records: Sequence[RawRecord]) -> None:
        self._records = sorted(records, key=lambda record: record.raw_id)

    def list_raw(self, limit: int, offset: int) -> Sequence[RawRecord]:
        return self._records[offset : offset + limit]

    def industry_of(self, channel_id: str) -> Optional[str]:
        return None


def test_raw_exists_from_source_covers_paged_fallback() -> None:
    records = [
        make_record(raw_id, channel_id, fetched_at, payload)
        for raw_id, channel_id, fetched_at, payload in DEMO_RECORDS
    ]
    exists = raw_exists_from_source(_PagedSource(records))
    assert exists("arch-0") is True
    assert exists("arch-2") is True
    assert exists("arch-9") is False


def test_webui_package_files_exist() -> None:
    assert [path.name for path in WEBUI_MODULES] == ["__init__.py", "app.py", "pages.py"]


#: 允许 import 的根模块：标准库 + 已提交的 atlas 包。没有 Web 框架、没有 sqlite3。
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "<relative>",
        "atlas",
        "html",
        "http",
        "json",
        "logging",
        "pathlib",
        "string",
        "threading",
        "typing",
        "urllib",
    }
)

FORBIDDEN_CALL_ATTRS = frozenset(
    {"execute", "executescript", "connect", "commit", "unlink", "rmtree", "makedirs", "mkdir"}
)


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("<relative>")
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _attribute_call_counts(tree: ast.AST, attribute: str) -> Dict[str, int]:
    """每个函数名 → 该函数体内调用 `.attribute(` 的次数（用于钉死写入口的位置）。"""
    counts: Dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            found = sum(
                1
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == attribute
            )
            if found:
                counts[node.name] = found
    return counts


def test_webui_uses_only_stdlib_and_the_atlas_package() -> None:
    """SPEC §2.11 / §5：无 Web 框架、零新增依赖。"""
    for path in WEBUI_MODULES:
        roots = _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
        assert roots <= ALLOWED_IMPORT_ROOTS, (path.name, sorted(roots - ALLOWED_IMPORT_ROOTS))
        assert "sqlite3" not in roots, "不得直接碰持久化实现（打标只经 atlas.labels）"


def test_webui_writes_no_sql_of_its_own() -> None:
    for path in WEBUI_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in FORBIDDEN_CALL_ATTRS, (path.name, node.func.attr)


def test_the_only_label_write_call_is_inside_storelabelaccess_record() -> None:
    """全包只有一处 `.add(`：`StoreLabelAccess.record` —— 唯一写入口。"""
    tree = ast.parse((WEBUI_PACKAGE / "app.py").read_text(encoding="utf-8"))
    assert _attribute_call_counts(tree, "add") == {"record": 1}


def test_the_write_method_is_reachable_only_from_the_post_handler() -> None:
    """`record` 只在 `_submit_label`（POST 分支）被调用；GET 路径拿不到写入口。"""
    tree = ast.parse((WEBUI_PACKAGE / "app.py").read_text(encoding="utf-8"))
    assert _attribute_call_counts(tree, "record") == {"_submit_label": 1}
