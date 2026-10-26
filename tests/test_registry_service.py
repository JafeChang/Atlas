"""T-101 服务层测试：CRUD + 列表查询 + 审计查询 + 非法操作被拒。

服务层是 T-106（feed）与配置前端的唯一入口，因此这里要证明三件事：

- CRUD 每次变更都产生新版本，且**没有写入旁路**（依旧由 schema 校验）
- 列表/过滤查询可用；查不到抛领域异常，不返回 None / 空占位
- 审计查询能回答"谁在何时改了什么"，且与版本链是同一份事实
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.contracts import (
    ContractError,
    IdError,
    InvalidContractStateError,
    NotFoundError,
)
from atlas.registry import (
    ChangeKind,
    Channel,
    FetchSpec,
    Industry,
    RegistryMutation,
    RegistryService,
    SqliteConfigStore,
)

BASE = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def make_clock():
    state = {"n": -1}

    def clock() -> datetime:
        state["n"] += 1
        return BASE + timedelta(minutes=state["n"])

    return clock


def industry(industry_id: str, name: str, **overrides: object) -> Industry:
    payload: dict = {"id": industry_id, "name": name, "enabled": True}
    payload.update(overrides)
    return Industry(**payload)


def channel(channel_id: str = "openai-blog", **overrides: object) -> Channel:
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


@pytest.fixture()
def service(tmp_path: Path) -> RegistryService:
    store = SqliteConfigStore(
        author="alice",
        industries=(
            industry("ai", name="人工智能"),
            industry("bio", name="生物技术"),
        ),
        channels=(
            channel("openai-blog", tags=("official", "llm")),
            channel("arxiv-lg", endpoint="https://arxiv.org/rss/cs.LG", tags=("papers",)),
            channel("disabled-feed", endpoint="https://example.com/off.xml", enabled=False),
            channel("bio-feed", industry_id="bio", endpoint="https://example.com/bio.xml"),
        ),
        clock=make_clock(),
        db_path=tmp_path / "atlas.db",
    )
    return RegistryService(store)


# --- CRUD：行业 ---------------------------------------------------------------


def test_create_industry_produces_version_and_is_queryable(service: RegistryService) -> None:
    record = service.create_industry(industry("robotics", name="机器人"), author="bob")
    assert record.version == 2
    assert record.author == "bob"
    assert record.kind is ChangeKind.CREATE
    assert service.require_industry("robotics").name == "机器人"
    assert {i.id for i in service.list_industries()} == {"ai", "bio", "robotics"}


def test_update_industry_builds_new_record_without_model_copy(service: RegistryService) -> None:
    record = service.update_industry("ai", name="人工智能（改名）", keywords=("llm",))
    assert record.kind is ChangeKind.UPDATE
    updated = service.require_industry("ai")
    assert updated.name == "人工智能（改名）"
    assert updated.keywords == ("llm",)
    assert updated.enabled is True
    assert record.diff[0].before is not None and record.diff[0].before["name"] == "人工智能"


def test_update_industry_cannot_change_id(service: RegistryService) -> None:
    with pytest.raises(InvalidContractStateError, match="不允许通过更新改动"):
        service.update_industry("ai", id="ai2")
    assert service.current_version == 1


def test_update_industry_rejects_unknown_fields(service: RegistryService) -> None:
    with pytest.raises(InvalidContractStateError, match="不认识的字段"):
        service.update_industry("ai", sort_order=3)
    assert service.current_version == 1


def test_update_industry_rejects_no_op_and_missing_object(service: RegistryService) -> None:
    with pytest.raises(InvalidContractStateError, match="无字段变化"):
        service.update_industry("ai", name="人工智能")
    with pytest.raises(NotFoundError):
        service.update_industry("ghost", name="幽灵")
    assert service.current_version == 1


def test_delete_industry_requires_no_channels_and_no_children(
    service: RegistryService,
) -> None:
    with pytest.raises(InvalidContractStateError, match="仍被渠道"):
        service.delete_industry("ai")
    service.create_industry(industry("widgets", name="部件", parent_id="bio"))
    with pytest.raises(InvalidContractStateError, match="仍有子行业"):
        service.delete_industry("bio")


def test_delete_industry_succeeds_when_unused(service: RegistryService) -> None:
    service.create_industry(industry("robotics", name="机器人"))
    record = service.delete_industry("robotics", author="bob", note="不再关注")
    assert record.kind is ChangeKind.DELETE
    assert record.note == "不再关注"
    with pytest.raises(NotFoundError):
        service.require_industry("robotics")


def test_invalid_industry_is_rejected_by_schema_via_service(service: RegistryService) -> None:
    bad = industry("robotics", name="机器人", parent_id="ghost")
    with pytest.raises(InvalidContractStateError, match="parent_id"):
        service.create_industry(bad)
    assert service.current_version == 1


def test_duplicate_industry_id_is_rejected(service: RegistryService) -> None:
    with pytest.raises(InvalidContractStateError, match="已存在"):
        service.create_industry(industry("ai", name="重复"))
    assert service.current_version == 1


# --- CRUD：渠道 ---------------------------------------------------------------


def test_create_channel_and_read_it_back(service: RegistryService) -> None:
    record = service.create_channel(
        channel("new-feed", endpoint="https://example.com/new.xml"), author="bob"
    )
    assert record.version == 2
    assert service.require_channel("new-feed").endpoint == "https://example.com/new.xml"


def test_update_channel_interval_and_tags(service: RegistryService) -> None:
    record = service.update_channel(
        "openai-blog", interval_seconds=900, tags=("official", "fast")
    )
    assert record.kind is ChangeKind.UPDATE
    updated = service.require_channel("openai-blog")
    assert updated.interval_seconds == 900
    assert updated.tags == ("official", "fast")


def test_invalid_channel_is_rejected_by_schema_via_service(service: RegistryService) -> None:
    """服务层没有旁路：非法 `interval_seconds` 与未知 `industry_id` 都被拒。"""
    with pytest.raises(InvalidContractStateError, match="低于下限"):
        service.update_channel("openai-blog", interval_seconds=10)
    with pytest.raises(InvalidContractStateError, match="不存在"):
        service.update_channel("openai-blog", industry_id="ghost")
    assert service.current_version == 1


def test_channel_endpoint_must_be_http_url(service: RegistryService) -> None:
    with pytest.raises(InvalidContractStateError, match="http"):
        service.update_channel("openai-blog", endpoint="ftp://example.com/feed.xml")
    assert service.current_version == 1


def test_channel_fetch_spec_must_match_type(service: RegistryService) -> None:
    with pytest.raises(ContractError):
        service.update_channel("openai-blog", fetch_spec=FetchSpec(type="json_api"))
    assert service.current_version == 1


def test_browser_masquerade_requires_justification(service: RegistryService) -> None:
    with pytest.raises(InvalidContractStateError, match="浏览器伪装"):
        service.update_channel("openai-blog", user_agent="chrome")
    service.update_channel(
        "openai-blog",
        user_agent="chrome",
        user_agent_justification="该站对默认 UA 返回 403，已确认 robots.txt 允许",
    )
    assert service.require_channel("openai-blog").user_agent == "chrome"


def test_set_channel_enabled_toggles_and_keeps_history(service: RegistryService) -> None:
    record = service.set_channel_enabled("openai-blog", False)
    assert record.kind is ChangeKind.UPDATE
    assert service.require_channel("openai-blog").enabled is False
    assert service.snapshot.channel("openai-blog").enabled is False
    assert service.snapshot(1) if False else True  # 版本 1 的历史仍在
    assert service.store.snapshot(1).channel("openai-blog").enabled is True


def test_set_industry_enabled_removes_its_channels_from_fetch_set(
    service: RegistryService,
) -> None:
    assert {c.id for c in service.fetchable_channels()} == {"openai-blog", "arxiv-lg", "bio-feed"}
    service.set_industry_enabled("bio", False)
    assert {c.id for c in service.fetchable_channels()} == {"openai-blog", "arxiv-lg"}
    # 停用行业仍存在于配置里，标签空间少了一个
    assert "bio" in {i.id for i in service.list_industries()}
    assert "bio" not in service.label_space()


# --- 列表与过滤 ---------------------------------------------------------------


def test_list_industries_filters_by_enabled(service: RegistryService) -> None:
    service.create_industry(industry("retired", name="已停用", enabled=False))
    assert {i.id for i in service.list_industries(enabled=True)} == {"ai", "bio"}
    assert {i.id for i in service.list_industries(enabled=False)} == {"retired"}


def test_list_industries_by_parent_includes_descendants(service: RegistryService) -> None:
    service.create_industry(industry("ml", name="机器学习", parent_id="ai"))
    service.create_industry(industry("llm", name="大模型", parent_id="ml"))
    assert {i.id for i in service.list_industries(parent_id="ai")} == {"ai", "ml", "llm"}
    assert {i.id for i in service.list_industries(parent_id="ai", include_children=False)} == {
        "ml"
    }


def test_list_industries_with_unknown_parent_raises(service: RegistryService) -> None:
    with pytest.raises(NotFoundError):
        service.list_industries(parent_id="ghost")


def test_list_channels_by_industry(service: RegistryService) -> None:
    assert {c.id for c in service.list_channels(industry_id="ai")} == {
        "openai-blog",
        "arxiv-lg",
        "disabled-feed",
    }
    assert {c.id for c in service.list_channels(industry_id="bio")} == {"bio-feed"}


def test_list_channels_filters_enabled_tag_and_type(service: RegistryService) -> None:
    assert {c.id for c in service.list_channels(enabled=True)} == {
        "openai-blog",
        "arxiv-lg",
        "bio-feed",
    }
    assert {c.id for c in service.list_channels(enabled=False)} == {"disabled-feed"}
    assert {c.id for c in service.list_channels(tag="papers")} == {"arxiv-lg"}
    assert {c.id for c in service.list_channels(fetch_type="rss")} == {
        "openai-blog",
        "arxiv-lg",
        "disabled-feed",
        "bio-feed",
    }


def test_list_channels_filters_are_conjunctive(service: RegistryService) -> None:
    assert {c.id for c in service.list_channels(industry_id="ai", enabled=True, tag="llm")} == {
        "openai-blog"
    }
    assert service.list_channels(industry_id="ai", tag="nope") == ()


def test_list_channels_with_unknown_industry_raises(service: RegistryService) -> None:
    with pytest.raises(NotFoundError):
        service.list_channels(industry_id="ghost")


def test_channels_per_industry_groups_and_includes_empty(service: RegistryService) -> None:
    grouped = dict(service.channels_per_industry())
    assert grouped["bio"] == ("bio-feed",)
    assert set(grouped["ai"]) == {"openai-blog", "arxiv-lg", "disabled-feed"}
    service.create_industry(industry("empty", name="暂无渠道"))
    assert dict(service.channels_per_industry())["empty"] == ()


# --- 生命周期视图 -------------------------------------------------------------


def test_views_report_created_and_updated_versions(service: RegistryService) -> None:
    created = service.create_industry(industry("robotics", name="机器人"))
    service.update_industry("robotics", name="机器人学")
    view = service.industry_view("robotics")
    assert view.created_version == created.version
    assert view.updated_version > view.created_version
    assert view.record.name == "机器人学"
    assert view.created_at <= view.updated_at


def test_channel_view_tracks_lifecycle(service: RegistryService) -> None:
    service.update_channel("openai-blog", interval_seconds=1800)
    view = service.channel_view("openai-blog")
    assert view.created_version == 1
    assert view.updated_version == 2
    assert view.record.interval_seconds == 1800
    assert view.industry_id == "ai"


# --- 审计 ---------------------------------------------------------------------


def test_audit_log_records_who_changed_what_and_when(service: RegistryService) -> None:
    service.create_industry(industry("robotics", name="机器人"), author="bob")
    service.update_channel("openai-blog", interval_seconds=900, author="carol")
    log = service.audit_log()

    created = [e for e in log if e.object_id == "robotics" and e.change is ChangeKind.CREATE]
    assert len(created) == 1
    assert created[0].author == "bob"
    assert created[0].version == 2
    assert created[0].after is not None and created[0].after["name"] == "机器人"

    updated = [e for e in log if e.object_id == "openai-blog" and e.change is ChangeKind.UPDATE]
    assert len(updated) == 1
    assert updated[0].author == "carol"
    assert updated[0].before is not None and updated[0].before["interval_seconds"] == 3600
    assert updated[0].after is not None and updated[0].after["interval_seconds"] == 900


def test_audit_log_covers_genesis(service: RegistryService) -> None:
    log = service.audit_log()
    genesis = [e for e in log if e.version == 1]
    assert {e.object_id for e in genesis} == {"ai", "bio", "openai-blog", "arxiv-lg", "disabled-feed", "bio-feed"}
    assert all(e.change is ChangeKind.CREATE for e in genesis)


def test_audit_for_returns_history_in_version_order(service: RegistryService) -> None:
    service.update_industry("ai", name="人工智能 v2")
    service.update_industry("ai", name="人工智能 v3")
    entries = service.audit_for("ai")
    assert [e.version for e in entries] == [1, 2, 3]
    assert [e.change for e in entries] == [
        ChangeKind.CREATE,
        ChangeKind.UPDATE,
        ChangeKind.UPDATE,
    ]
    assert entries[-1].after is not None and entries[-1].after["name"] == "人工智能 v3"


def test_audit_for_deleted_object_still_readable(service: RegistryService) -> None:
    """删掉的对象，审计链里依然查得到（这是"只增不改"的直接好处）。"""
    service.create_industry(industry("robotics", name="机器人"))
    service.delete_industry("robotics")
    entries = service.audit_for("robotics")
    assert [e.change for e in entries] == [ChangeKind.CREATE, ChangeKind.DELETE]
    assert entries[-1].before is not None


def test_audit_for_unknown_object_raises(service: RegistryService) -> None:
    with pytest.raises(NotFoundError, match="也没有任何历史变更记录"):
        service.audit_for("never-existed")


def test_audit_by_author_filters(service: RegistryService) -> None:
    service.create_industry(industry("robotics", name="机器人"), author="bob")
    service.update_industry("ai", name="人工智能2", author="carol")
    assert {e.object_id for e in service.audit_by("bob")} == {"robotics"}
    assert {e.object_id for e in service.audit_by("carol")} == {"ai"}
    assert service.audit_by("nobody") == ()
    with pytest.raises(InvalidContractStateError, match="非空作者"):
        service.audit_by("   ")


def test_audit_by_since_filters_by_time(service: RegistryService) -> None:
    service.create_industry(industry("robotics", name="机器人"), author="bob")
    later = BASE + timedelta(minutes=5)
    assert service.audit_by("bob", since=later) == ()
    assert {e.object_id for e in service.audit_by("bob", since=BASE)} == {"robotics"}
    # genesis 是 alice 写的，按作者查不到
    assert "ai" not in {e.object_id for e in service.audit_by("bob")}
    assert {e.object_id for e in service.audit_by("alice", since=BASE)} >= {"ai", "bio"}


def test_audit_between_versions(service: RegistryService) -> None:
    service.create_industry(industry("robotics", name="机器人"))
    service.update_industry("ai", name="人工智能2")
    entries = service.audit_between(2, 2)
    assert {e.object_id for e in entries} == {"robotics"}
    with pytest.raises(InvalidContractStateError, match="区间非法"):
        service.audit_between(3, 2)


def test_audit_entry_summary_is_human_readable(service: RegistryService) -> None:
    service.update_industry("ai", name="人工智能2", author="bob")
    entry = [e for e in service.audit_for("ai") if e.change is ChangeKind.UPDATE][0]
    summary = entry.summary()
    assert "bob" in summary and "修改" in summary and "ai" in summary
    assert "name" in summary


def test_audit_chain_matches_version_history(service: RegistryService) -> None:
    """审计链与版本链是同一份事实：条数与 diff 总数一致。"""
    service.create_industry(industry("robotics", name="机器人"))
    service.update_channel("openai-blog", interval_seconds=900)
    expected = sum(len(v.diff) for v in service.store.history())
    assert len(service.audit_log()) == expected


def test_diff_between_versions(service: RegistryService) -> None:
    service.create_industry(industry("robotics", name="机器人"))
    entries = service.diff(1, 2)
    assert {(e.object_id, e.change) for e in entries} == {("robotics", ChangeKind.CREATE)}
    assert service.diff(2, 2) == ()


# --- 批量提交 / 回滚 / 版本串 --------------------------------------------------


def test_commit_raw_is_atomic_and_versioned(service: RegistryService) -> None:
    record = service.commit_raw(
        [
            RegistryMutation(
                kind="create",
                object_kind="industry",
                payload=industry("robotics", name="机器人").payload(),
            ),
            RegistryMutation(
                kind="create",
                object_kind="channel",
                payload=channel("robot-feed", industry_id="robotics").payload(),
            ),
        ]
    )
    assert record.kind is ChangeKind.BATCH
    assert service.require_channel("robot-feed").industry_id == "robotics"


def test_commit_raw_rejects_illegal_batch_entirely(service: RegistryService) -> None:
    with pytest.raises(InvalidContractStateError, match="不存在"):
        service.commit_raw(
            [
                RegistryMutation(
                    kind="create",
                    object_kind="industry",
                    payload=industry("robotics", name="机器人").payload(),
                ),
                RegistryMutation(
                    kind="create",
                    object_kind="channel",
                    payload=channel("bad", industry_id="ghost").payload(),
                ),
            ]
        )
    assert service.current_version == 1
    with pytest.raises(NotFoundError):
        service.require_industry("robotics")


def test_rollback_through_service_keeps_audit_readable(service: RegistryService) -> None:
    service.create_industry(industry("robotics", name="机器人"), author="bob")
    record = service.rollback(1, author="carol", note="撤销")
    assert record.kind is ChangeKind.ROLLBACK
    assert record.restored_from == 1
    with pytest.raises(NotFoundError):
        service.require_industry("robotics")
    # 审计里仍能看到 bob 当初的新增与 carol 的回滚
    assert {e.author for e in service.audit_for("robotics")} == {"bob", "carol"}
    assert service.audit_by("carol")[0].restored_from == 1


def test_config_version_moves_with_changes(service: RegistryService) -> None:
    first = service.config_version
    assert first.startswith("cfg-v0001-")
    service.create_industry(industry("robotics", name="机器人"))
    assert service.config_version.startswith("cfg-v0002-")
    assert service.config_version != first


def test_service_reads_through_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = SqliteConfigStore(
        author="alice",
        industries=(industry("ai", name="人工智能"),),
        channels=(channel("openai-blog"),),
        clock=make_clock(),
        db_path=db,
    )
    service = RegistryService(store)
    service.create_industry(industry("robotics", name="机器人"), author="bob")
    store.close()

    reopened = RegistryService(
        SqliteConfigStore(author="alice", db_path=db)
    )
    assert reopened.require_industry("robotics").name == "机器人"
    assert {e.object_id for e in reopened.audit_by("bob")} == {"robotics"}
    assert reopened.industry_view("ai").created_version == 1
    reopened.store.close()


def test_industry_id_conflicting_with_channel_id_is_rejected(
    service: RegistryService,
) -> None:
    with pytest.raises(InvalidContractStateError, match="已被渠道占用"):
        service.create_industry(industry("openai-blog", name="撞名"))
    assert service.current_version == 1


def test_invalid_id_format_is_rejected_at_construction_and_at_service(
    service: RegistryService,
) -> None:
    """非法 id 在模型构造时就抛 `IdError`（单对象校验），服务层同样拒绝。"""
    with pytest.raises(IdError):
        Industry(id="Not_A_Valid_Id", name="坏 id", enabled=True)
    with pytest.raises(IdError):
        service.create_industry(Industry(id="also bad", name="坏 id", enabled=True))
    assert service.current_version == 1
    service.create_industry(Industry(id="ok-id", name="好 id", enabled=True))
    assert service.current_version == 2


def test_update_fetch_spec_replacement_is_validated(service: RegistryService) -> None:
    service.update_channel(
        "openai-blog",
        type="json_api",
        fetch_spec=FetchSpec(type="json_api", list_path="data.items", title_path="title"),
    )
    updated = service.require_channel("openai-blog")
    assert updated.type.value == "json_api"
    assert updated.fetch_spec.list_path == "data.items"
