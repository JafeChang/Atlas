import hashlib
import json
import sqlite3

import pytest

from atlas.cognition import (
    ArticleBrief,
    BriefClient,
    BriefStore,
    brief_source,
    display_brief,
    read_articles,
)
from atlas.cognition.briefs import clip_brief, plain_text
from tests.test_cognition_support import fake_config

BODY = "The paper proposes an algorithm for improving image recognition using limited training data. It evaluates accuracy and training efficiency."


def response(items):
    return type(
        "Response",
        (),
        {
            "status_code": 200,
            "json": lambda _: {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"briefs": items})},
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20},
            },
        },
    )()


def test_text_cleanup_char_limit_and_native_title_detection():
    assert plain_text("<p>正文 <b>重点</b></p><script>alert(1)</script>") == "正文 重点"
    title_only = brief_source("Article title", "")
    assert not title_only.has_body and title_only.kind == "title"
    assert brief_source("Article title", BODY).has_body
    assert len(clip_brief("中文简介" * 100)) <= 300
    native = brief_source("中文研究成果", BODY)
    assert native.native_chinese
    assert display_brief(native, {}) == ("", "简介待生成")
    assert (
        native.key != brief_source(native.title, BODY, url="https://example.test/").key
    )


def test_formal_database_columns_read_only_and_atomic_rejection(tmp_path):
    path = tmp_path / "atlas.db"
    assert read_articles(path) == {} and not path.exists()
    source = brief_source("A new result", BODY)
    store = BriefStore(path)
    store.record(
        [source],
        {source.key: ArticleBrief("一项新的研究成果", "研究提出了一种新算法。" * 60)},
        model="test",
    )
    store.close()
    before = hashlib.sha256(path.read_bytes()).digest()
    rows = read_articles(path)
    assert rows[source.key].title_zh == "一项新的研究成果"
    text, origin = display_brief(source, rows)
    assert len(text) <= 300 and origin == "AI 中文简介"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as db:
        assert {"title_zh", "summary_zh"}.issubset(
            {r[1] for r in db.execute("PRAGMA table_info(feed_articles)")}
        )
    store = BriefStore(path)
    try:
        with pytest.raises(ValueError, match="一一对应"):
            store.record([source], {}, model="test")
        with pytest.raises(ValueError, match="有效中文简介"):
            store.record(
                [source],
                {source.key: ArticleBrief("新的研究", "English only")},
                model="test",
            )
        # 同一路径合法输入成功；坏组不会覆盖既有结果。
        assert read_articles(path)[source.key].summary_zh == text
        store.record(
            [source],
            {source.key: ArticleBrief("研究成果", "该方法改善图像识别。")},
            model="test",
        )
    finally:
        store.close()
    assert read_articles(path)[source.key].summary_zh == "该方法改善图像识别。"


def test_client_reads_body_and_returns_separate_title_and_summary(monkeypatch):
    source = brief_source("Example", BODY)
    observed = {}

    def post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return response(
            [
                {
                    "id": "1",
                    "title_zh": "图像识别方法",
                    "summary_zh": "该研究使用有限的训练数据改善图像识别。",
                }
            ]
        )

    monkeypatch.setattr("atlas.cognition.briefs.requests.post", post)
    client = BriefClient(fake_config())
    texts, usage = client.generate([source])
    assert texts[source.key].title_zh == "图像识别方法"
    assert usage["prompt_tokens"] == 40
    assert "tools" not in observed["json"]
    assert observed["json"]["thinking"] == {"type": "disabled"}
    assert BODY in observed["json"]["messages"][1]["content"]
    assert client.config.api_key not in json.dumps(observed["json"])


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"id": "wrong", "title_zh": "译题", "summary_zh": "中文简介"}],
        [{"id": "1", "title_zh": "译题", "summary_zh": "English only"}],
        [{"id": "1", "title_zh": "", "summary_zh": "中文简介"}],
        [{"id": "1", "text": "旧版响应"}],
    ],
)
def test_invalid_or_old_results_are_rejected(monkeypatch, items):
    monkeypatch.setattr(
        "atlas.cognition.briefs.requests.post", lambda *a, **k: response(items)
    )
    with pytest.raises(ValueError):
        BriefClient(fake_config()).generate([brief_source("Example", BODY)])


def test_chinese_article_keeps_title_and_still_gets_model_summary(
    tmp_path, monkeypatch
):
    source = brief_source(
        "研究提出新方法",
        "本文提出一种图像识别方法，使用有限的训练数据，评估识别效果与训练效率。" * 15,
    )
    monkeypatch.setattr(
        "atlas.cognition.briefs.requests.post",
        lambda *a, **k: response(
            [
                {
                    "id": "1",
                    "title_zh": "重复译题",
                    "summary_zh": "研究使用有限数据训练图像识别算法，并评估识别表现与效率。",
                }
            ]
        ),
    )
    results, _ = BriefClient(fake_config()).generate([source])
    assert results[source.key].title_zh == ""
    store = BriefStore(tmp_path / "atlas.db")
    store.record([source], results, model="test")
    store.close()
    assert read_articles(tmp_path / "atlas.db")[source.key].summary_zh


def test_title_only_is_never_sent_to_model_or_saved_as_summary(tmp_path, monkeypatch):
    source = brief_source("A title", "", url="https://example.test/article")
    calls = []
    monkeypatch.setattr(
        "atlas.cognition.briefs.requests.post", lambda *a, **k: calls.append(1)
    )
    with pytest.raises(ValueError, match="正文"):
        BriefClient(fake_config()).generate([source])
    store = BriefStore(tmp_path / "atlas.db")
    try:
        with pytest.raises(ValueError, match="缺少正文"):
            store.record(
                [source],
                {source.key: ArticleBrief("标题", "这只是标题翻译。")},
                model="test",
            )
    finally:
        store.close()
    assert not calls


def test_cli_fetches_original_for_missing_abstract_persists_and_resumes(
    tmp_path, monkeypatch
):
    from atlas.archive import open_archive
    from atlas.briefs_cli import main
    from atlas.collect.article import ArticleContent
    from atlas.contracts import RawRecord

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>New method</title><link>https://example.test/article</link></item></channel></rss>"
    record = RawRecord.create(
        channel_id="test",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    fetched, calls = [], []

    def read(self, url):
        fetched.append(url)
        return ArticleContent(BODY, url)

    def generate(self, sources):
        calls.append(len(sources))
        assert sources[0].body == BODY
        if len(calls) == 1:
            raise RuntimeError("简介模型请求失败：HTTP 503")
        return {
            s.key: ArticleBrief(
                "一种新方法", "研究提出改进图像识别的方法，并评估训练效率。"
            )
            for s in sources
        }, {}

    monkeypatch.setattr("atlas.briefs_cli.ArticleReader.read", read)
    monkeypatch.setattr(BriefClient, "generate", generate)
    monkeypatch.setattr("atlas.briefs_cli.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "atlas.briefs_cli.CognitionConfig.from_env", lambda **kw: fake_config()
    )
    args = ["--store-root", str(root), "--report-dir", str(tmp_path / "report")]
    assert main(args) == 0
    before = hashlib.sha256((root / "atlas.db").read_bytes()).digest()
    assert main(args) == 0
    assert calls == [1, 1] and fetched == ["https://example.test/article"]
    assert hashlib.sha256((root / "atlas.db").read_bytes()).digest() == before
    row = next(iter(read_articles(root / "atlas.db").values()))
    assert (
        row.body_source == "original"
        and row.article_text == BODY
        and row.title_zh == "一种新方法"
    )
    with open_archive(root) as archive:
        assert archive.get_content(record.raw_id) == payload


def test_failed_original_is_recorded_and_not_summarized(tmp_path, monkeypatch):
    from atlas.archive import open_archive
    from atlas.briefs_cli import main
    from atlas.collect.article import ArticleReadError
    from atlas.contracts import RawRecord

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>Title</title><link>https://example.test/article</link></item></channel></rss>"
    record = RawRecord.create(
        channel_id="test",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)

    def unavailable(self, url):
        raise ArticleReadError("原文 HTTP 403")

    monkeypatch.setattr("atlas.briefs_cli.ArticleReader.read", unavailable)
    monkeypatch.setattr(
        "atlas.briefs_cli.CognitionConfig.from_env",
        lambda **kw: pytest.fail("不能为读取失败调用模型"),
    )
    assert (
        main(["--store-root", str(root), "--report-dir", str(tmp_path / "report")]) == 0
    )
    row = next(iter(read_articles(root / "atlas.db").values()))
    assert row.status == "unavailable" and row.error == "原文 HTTP 403"
    assert row.summary_zh == "" and row.title_zh == ""


def test_cli_splits_invalid_model_batches_and_saves_exact_article_correspondence(
    tmp_path, monkeypatch
):
    from atlas.archive import open_archive
    from atlas.briefs_cli import main
    from atlas.contracts import RawRecord

    root = tmp_path / "store"
    payload = (
        "<rss><channel>"
        + "".join(
            f"<item><title>Article {i}</title><link>https://example.test/{i}</link><description>{BODY}</description></item>"
            for i in range(4)
        )
        + "</channel></rss>"
    ).encode()
    record = RawRecord.create(
        channel_id="test",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)

    def generate(self, sources):
        if len(sources) > 1:
            raise ValueError("malformed JSON")
        source = sources[0]
        number = source.title.split()[-1]
        return {
            source.key: ArticleBrief(
                "文章" + number, "这是文章" + number + "的中文简介，介绍图像识别研究。"
            )
        }, {}

    monkeypatch.setattr(BriefClient, "generate", generate)
    monkeypatch.setattr(
        "atlas.briefs_cli.CognitionConfig.from_env", lambda **kw: fake_config()
    )
    assert (
        main(["--store-root", str(root), "--report-dir", str(tmp_path / "report")]) == 0
    )
    rows = read_articles(root / "atlas.db")
    assert len(rows) == 4 and all(r.status == "ready" for r in rows.values())
    for row in rows.values():
        assert row.title_zh[-1] in row.summary_zh
    assert len({r.title_zh for r in rows.values()}) == 4
