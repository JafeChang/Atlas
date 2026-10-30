"""在现有 Web UI 中装配信源管理；GET 只读，启停走版本化注册表。"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

from atlas.registry import open_store
from atlas.registry.versioning import RegistryMutation

from .models import Source, collection_source, probe_verified
from .source_page import registered_source_view, render_sources
from .store import AluminiumStore, has_aluminium

AI_INDUSTRIES = frozenset(
    {
        "machine-learning",
        "computer-vision",
        "natural-language",
        "statistical-learning",
        "data-science",
    }
)
REGISTERED_NAMES = {
    "ai-news-blog": "AI News",
    "ai-techpark": "AI TechPark",
    "arxiv-computer-vision": "arXiv · 计算机视觉",
    "arxiv-machine-learning": "arXiv · 机器学习",
    "arxiv-natural-language": "arXiv · 自然语言处理",
    "arxiv-statistical-learning": "arXiv · 统计学习",
    "google-ai-blog": "Google AI Blog",
    "hacker-news-frontpage": "Hacker News",
    "kdnuggets": "KDnuggets",
    "marktechpost": "MarkTechPost",
    "openai-blog": "OpenAI Blog",
    "synced-review": "Synced Review",
    "venturebeat-ai": "VentureBeat AI",
}


def _group_name(industry_id, industry_name):
    return "AI 信源" if industry_id in AI_INDUSTRIES else (industry_name or "其他来源")


class SourceManagement:
    def __init__(self, db_path, *, lock_path=".tmp_atlas/collection.lock"):
        self.db_path = Path(db_path)
        self.lock_path = Path(lock_path)

    def channel_names(self):
        """显示名称来自导入目录；浏览只读，不改变渠道身份或配置历史。"""
        if not has_aluminium(self.db_path):
            return {}
        with AluminiumStore(self.db_path, read_only=True) as store:
            return {
                row["channel_id"]: json.loads(row["original_json"])["name"]
                for row in store.sources()
                if row["channel_id"]
            }

    def channel_groups(self):
        """来源分组按渠道配置归属读取，AI 子行业在来源菜单中合并显示。"""
        if not self.db_path.is_file():
            return {}
        with AluminiumStore(self.db_path, read_only=True) as store:
            tables = {
                row[0]
                for row in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('channels','industries')"
                )
            }
            if tables != {"channels", "industries"}:
                return {}
            return {
                row["channel_id"]: _group_name(row["industry_id"], row["name"])
                for row in store.db.execute(
                    "SELECT c.channel_id,c.industry_id,i.name FROM channels c "
                    "LEFT JOIN industries i ON i.industry_id=c.industry_id"
                )
            }

    def render(self, message=""):
        if not self.db_path.is_file():
            return render_sources([], {}, 0, [], message or "尚未导入行业信源。")
        with AluminiumStore(self.db_path, read_only=True) as store:
            tables = {
                r[0]
                for r in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"channels", "industries", "config_versions"} <= tables:
                return render_sources([], {}, 0, [], message or "尚未导入行业信源。")
            has_catalog = "aluminium_sources" in tables
            rows = store.status() if has_catalog else []
            channels = {
                r["channel_id"]: json.loads(r["payload"])
                for r in store.db.execute("SELECT channel_id,payload FROM channels")
            }
            version = store.db.execute(
                "SELECT MAX(version) FROM config_versions"
            ).fetchone()[0]
            observations = store.intelligence() if has_catalog else []
            industries = {
                r["industry_id"]: r["name"]
                for r in store.db.execute("SELECT industry_id,name FROM industries")
            }
            catalog_channels = {r["channel_id"] for r in rows if r["channel_id"]}
            latest, successes = {}, {}
            if "raw_records" in tables:
                for record in store.db.execute(
                    "SELECT channel_id,fetched_at,http_status FROM raw_records ORDER BY fetched_at DESC,raw_id DESC"
                ):
                    cid = record["channel_id"]
                    latest.setdefault(cid, dict(record))
                    if (
                        record["http_status"] is not None
                        and 200 <= record["http_status"] < 300
                    ):
                        successes.setdefault(cid, dict(record))
            registered = [
                registered_source_view(
                    channel,
                    name=REGISTERED_NAMES.get(cid, cid),
                    group=_group_name(
                        channel["industry_id"], industries.get(channel["industry_id"])
                    ),
                    industry=industries.get(
                        channel["industry_id"], channel["industry_id"]
                    ),
                    latest=latest.get(cid),
                    success=successes.get(cid),
                )
                for cid, channel in channels.items()
                if cid not in catalog_channels
            ]
        return render_sources(
            rows,
            channels,
            version,
            observations,
            message,
            registered=registered,
            show_intelligence=has_catalog,
        )

    def toggle(self, sid, enabled, version, actor):
        if enabled not in ("0", "1"):
            raise ValueError("enabled 必须为 0 或 1")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Linux 的 flock 可在只读 FD 上使用独占锁；Web 服务的工作区为只读挂载，
        # 已有 cron 锁无须写权限。首次本地初始化时才创建文件。
        with self.lock_path.open("r" if self.lock_path.exists() else "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("采集或配置任务正在运行，请稍后重试") from exc
            with AluminiumStore(self.db_path, read_only=True) as store:
                has_catalog = store.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aluminium_sources'"
                ).fetchone()
                row = store.source(sid) if has_catalog else None
                if row:
                    if not row["channel_id"]:
                        raise ValueError("信源尚未配置可采集渠道")
                    source = collection_source(
                        Source.model_validate_json(row["original_json"]),
                        row["connection"],
                    )
                    probe = store.latest_probe(row["source_id"])
                    channel_payload = json.loads(
                        store.db.execute(
                            "SELECT payload FROM channels WHERE channel_id=?",
                            (row["channel_id"],),
                        ).fetchone()[0]
                    )
                    if enabled == "1" and (
                        source.pending_reason
                        or not probe_verified(probe, row["connection"], channel_payload)
                    ):
                        raise ValueError(
                            source.pending_reason or "启用前需要完成连通性及结构验证"
                        )
                    channel_id = row["channel_id"]
                else:
                    channel_id = sid
                    if not store.db.execute(
                        "SELECT 1 FROM channels WHERE channel_id=?", (sid,)
                    ).fetchone():
                        raise ValueError("未知信源，未配置可采集渠道")
            with open_store(self.db_path, author=actor) as registry:
                if str(registry.current_version) != version:
                    raise ValueError("配置已更新，请刷新页面后重试")
                channel = next(
                    c for c in registry.current.channels if c.id == channel_id
                )
                if channel.enabled != (enabled == "1"):
                    payload = channel.payload()
                    payload["enabled"] = enabled == "1"
                    registry.commit(
                        [
                            RegistryMutation(
                                kind="update",
                                object_kind="channel",
                                object_id=channel.id,
                                payload=payload,
                            )
                        ],
                        note="信源管理：单源启停；历史内容保留",
                    )
