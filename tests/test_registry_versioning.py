"""T-004 版本化与回滚测试（SPEC §2.9「版本化与回滚」）。

要点：每次变更产生新版本（作者 / 时间 / diff）；回滚到任一历史版本，
且回滚**不重写历史**；被标签引用的行业不得修改或删除；
采集任务能取到稳定的 `config_version` 串（SPEC §3）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atlas.contracts import IdError, InvalidContractStateError, NotFoundError, VersionError
from atlas.registry import (
    ChangeKind,
    Channel,
    ConfigStore,
    FetchSpec,
    Industry,
    RegistryMutation,
)


def make_clock():
    """确定性时钟：第 n 次调用返回 T0 + n 分钟。"""
    base = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    state = {"n": -1}

    def clock() -> datetime:
        state["n"] += 1
        return base + timedelta(minutes=state["n"])

    return clock


def industry(industry_id: str, name: str, **overrides: object) -> Industry:
    """便捷构造：`enabled` 是 SPEC §2.9 的必填项，测试里默认显式给 True。"""
    payload: dict = {"id": industry_id, "name": name, "enabled": True}
    payload.update(overrides)
    return Industry(**payload)


def make_channel(channel_id: str = "openai-blog", **overrides: object) -> Channel:
    payload: dict = {
        "id": channel_id,
        "industry_id": "ai",
        "type": "rss",
        "endpoint": f"https://example.com/{channel_id}.xml",
        "fetch_spec": FetchSpec(type="rss"),
        "interval_seconds": 3600,
        "enabled": True,
    }
    payload.update(overrides)
    return Channel(**payload)


def make_store(**kwargs: object) -> ConfigStore:
    params: dict = {
        "author": "alice",
        "industries": (industry("ai", name="人工智能"),),
        "channels": (make_channel(),),
        "clock": make_clock(),
    }
    params.update(kwargs)
    return ConfigStore(**params)  # type: ignore[arg-type]


# --- genesis 与当前快照 --------------------------------------------------------


def test_genesis_is_version_one_with_author_and_timestamp() -> None:
    store = make_store()
    genesis = store.version(1)
    assert genesis.version == 1
    assert genesis.author == "alice"
    assert genesis.parent_version is None
    assert genesis.created_at == datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    assert {d.object_id for d in genesis.diff} == {"ai", "openai-blog"}
    assert all(d.change is ChangeKind.CREATE for d in genesis.diff)


def test_genesis_rejects_invalid_initial_registry() -> None:
    with pytest.raises(InvalidContractStateError, match="不存在"):
        ConfigStore(
            author="alice",
            industries=(industry("ai", name="AI"),),
            channels=(make_channel(industry_id="ghost"),),
        )


def test_empty_author_rejected() -> None:
    with pytest.raises(InvalidContractStateError):
        ConfigStore(author="   ")


# --- 每次变更产生新版本 --------------------------------------------------------


def test_create_industry_produces_new_version_with_diff() -> None:
    store = make_store()
    record = store.create_industry(industry("biotech", name="生物技术", keywords=("crispr",)))

    assert record.version == 2
    assert record.parent_version == 1
    assert record.kind is ChangeKind.CREATE
    assert record.author == "alice"
    assert record.created_at == datetime(2026, 9, 25, 12, 1, tzinfo=timezone.utc)
    assert len(record.diff) == 1
    entry = record.diff[0]
    assert (entry.object_kind, entry.object_id, entry.change) == ("industry", "biotech", ChangeKind.CREATE)
    assert entry.before is None
    assert entry.after is not None and entry.after["keywords"] == ["crispr"]
    assert [v.version for v in store.versions] == [1, 2]


def test_update_records_before_and_after() -> None:
    store = make_store()
    record = store.update_industry(industry("ai", name="人工智能（改名）"))

    assert record.kind is ChangeKind.UPDATE
    entry = record.diff[0]
    assert entry.before is not None and entry.before["name"] == "人工智能"
    assert entry.after is not None and entry.after["name"] == "人工智能（改名）"
    assert store.current.industry("ai").name == "人工智能（改名）"


def test_delete_channel_after_deleting_nothing_else() -> None:
    store = make_store()
    record = store.delete_channel("openai-blog")
    assert record.kind is ChangeKind.DELETE
    assert record.diff[0].after is None
    assert store.current.channels == ()


def test_history_is_ordered_and_immutable() -> None:
    store = make_store()
    store.create_industry(industry("biotech", name="生物"))
    store.update_industry(industry("biotech", name="生物技术"))
    history = store.history()
    assert [v.version for v in history] == [1, 2, 3]
    assert history[0].snapshot_digest != history[2].snapshot_digest


def test_no_op_commit_is_rejected_loudly() -> None:
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="没有任何变更"):
        store.commit([])
    with pytest.raises(InvalidContractStateError, match="无字段变化"):
        store.update_industry(industry("ai", name="人工智能"))
    assert store.current_version == 1


def test_duplicate_create_rejected_and_no_partial_version() -> None:
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="已存在"):
        store.create_industry(industry("ai", name="重复"))
    assert store.current_version == 1


def test_batch_with_one_bad_mutation_leaves_no_partial_state() -> None:
    """失败不留半成品（SPEC §3）：一个 mutation 非法 → 整批不产生版本。"""
    store = make_store()
    mutations = [
        RegistryMutation(
            kind="create",
            object_kind="industry",
            payload=industry("biotech", name="生物").payload(),
        ),
        RegistryMutation(
            kind="create",
            object_kind="channel",
            payload=make_channel("bad", industry_id="ghost").payload(),
        ),
    ]
    with pytest.raises(InvalidContractStateError, match="不存在"):
        store.commit(mutations)
    assert store.current_version == 1
    with pytest.raises(NotFoundError):
        store.current.industry("biotech")


def test_mixed_batch_is_marked_batch() -> None:
    store = make_store()
    record = store.commit(
        [
            RegistryMutation(
                kind="create",
                object_kind="industry",
                payload=industry("biotech", name="生物").payload(),
            ),
            RegistryMutation(kind="delete", object_kind="channel", object_id="openai-blog"),
        ]
    )
    assert record.kind is ChangeKind.BATCH
    assert {d.object_id for d in record.diff} == {"biotech", "openai-blog"}


# --- id 不可改 / 被引用不得删除 ------------------------------------------------


def test_channel_update_cannot_change_id() -> None:
    store = make_store()
    with pytest.raises(IdError, match="不得改动 id"):
        store.commit(
            [
                RegistryMutation(
                    kind="update",
                    object_kind="channel",
                    object_id="openai-blog",
                    payload=make_channel("renamed").payload(),
                )
            ]
        )


def test_referenced_industry_cannot_be_deleted() -> None:
    store = make_store(label_references=lambda: {"ai"})
    with pytest.raises(InvalidContractStateError, match="已被标签引用"):
        store.delete_industry("ai")
    assert store.current_version == 1


def test_referenced_industry_delete_is_reported_in_message() -> None:
    store = make_store(label_references=lambda: ("ai",))
    with pytest.raises(InvalidContractStateError, match="SPEC §2.9 校验规则 1"):
        store.delete_industry("ai")


def test_unreferenced_industry_can_be_deleted() -> None:
    store = make_store(label_references=lambda: {"ai"})
    store.create_industry(industry("biotech", name="生物"))
    record = store.delete_industry("biotech")
    assert record.kind is ChangeKind.DELETE
    assert store.label_space() == ("ai",)


def test_industry_with_channels_cannot_be_deleted() -> None:
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="仍被渠道"):
        store.delete_industry("ai")


def test_update_missing_object_raises_not_found() -> None:
    store = make_store()
    with pytest.raises(NotFoundError):
        store.update_channel(make_channel("ghost"))


# --- 回滚 ----------------------------------------------------------------------


def test_rollback_restores_previous_registry_as_new_version() -> None:
    store = make_store()
    store.create_industry(industry("biotech", name="生物"))
    assert store.current_version == 2

    record = store.rollback(1, author="bob", note="回退到种子配置")

    assert record.version == 3
    assert record.kind is ChangeKind.ROLLBACK
    assert record.restored_from == 1
    assert record.author == "bob"
    assert record.note == "回退到种子配置"
    assert store.current.digest() == store.snapshot(1).digest()
    assert store.current_version == 3
    # 历史版本依然可读，未被重写
    assert [v.version for v in store.history()] == [1, 2, 3]
    assert store.snapshot(2).industry("biotech").name == "生物"


def test_rollback_diff_describes_what_changed() -> None:
    store = make_store()
    store.create_industry(industry("biotech", name="生物"))
    record = store.rollback(1)
    kinds = {(d.object_id, d.change) for d in record.diff}
    assert kinds == {("biotech", ChangeKind.DELETE)}


def test_rollback_to_current_is_rejected() -> None:
    store = make_store()
    with pytest.raises(VersionError, match="即当前版本"):
        store.rollback(1)


def test_rollback_to_unknown_version_raises_not_found() -> None:
    store = make_store()
    with pytest.raises(NotFoundError):
        store.rollback(99)


def test_rollback_is_blocked_when_it_would_drop_a_referenced_industry() -> None:
    referenced: set[str] = set()
    store = make_store(label_references=lambda: set(referenced))
    store.create_industry(industry("biotech", name="生物"))
    store.create_channel(make_channel("biotech-feed", industry_id="biotech"))
    # 有人在 biotech 上打了标签之后，回滚到 v1（没有 biotech）必须被拒绝
    referenced.add("biotech")
    with pytest.raises(InvalidContractStateError, match="已被标签引用"):
        store.rollback(1)
    assert store.current_version == 3


def test_version_chain_is_linear_and_ancestry_is_readable() -> None:
    store = make_store()
    store.create_industry(industry("a1", name="A1"))
    store.rollback(1)
    parents = [(v.version, v.parent_version) for v in store.history()]
    assert parents == [(1, None), (2, 1), (3, 2)]


# --- config_version（SPEC §3：采集任务必须记录配置版本）------------------------


def test_config_version_string_is_stable_and_content_addressed() -> None:
    store = make_store()
    first = store.current.config_version
    assert first == store.current.config_version
    assert first.startswith("cfg-v0001-")
    assert len(first.split("-")[-1]) == 12

    store.create_industry(industry("biotech", name="生物"))
    assert store.current.config_version != first
    assert store.current.config_version.startswith("cfg-v0002-")


def test_identical_content_in_two_stores_yields_same_digest() -> None:
    left = make_store()
    right = make_store()
    assert left.current.digest() == right.current.digest()
    assert left.current.config_version == right.current.config_version


# --- 闭环约束：标签空间来自配置，代码里没有行业枚举 ----------------------------


def test_label_space_reads_from_enabled_config_only() -> None:
    store = make_store()
    store.commit(
        [
            RegistryMutation(
                kind="create",
                object_kind="industry",
                payload=industry("biotech", name="生物").payload(),
            ),
            RegistryMutation(
                kind="create",
                object_kind="industry",
                payload=industry("retired", name="停用行业", enabled=False).payload(),
            ),
        ]
    )
    assert set(store.label_space()) == {"ai", "biotech"}
    assert "retired" not in store.label_space()
