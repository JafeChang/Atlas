"""T-111 健康状态持久化：`catalog_health` 表 + 写回注册表（SPEC §2.10 表归属）。

`catalog_health` 归本任务（SPEC §2.10 表归属登记）。两条硬性边界：

1. **只增不改**：表上只有 `INSERT`；用 SQL 触发器拒绝 `UPDATE` / `DELETE`
   （与 `config_versions`、`raw_records` 同一做法：不变量在存储层强制，而非靠约定）。
   因此**最新一条即当前状态** —— "当前是否健康"这个问题在 SQL 层就是一个
   `ORDER BY id DESC LIMIT 1`，没有"状态字段被就地改写"的可能。
2. **共用 DB 的 DDL 边界**：`data/store/atlas.db` 由多个域共用，本模块**只**执行
   `CREATE TABLE IF NOT EXISTS catalog_health` + 自己的索引 + 自己的触发器。
   不 `DROP`、不改别的域的表、不 import 其它域的持久化模块
   （`DEFAULT_DB_PATH` 在本模块自己声明，正是因为"共用同一个库文件"不等于
   "可以依赖别人的持久化实现"）。

**写回注册表**（任务书的"标记"）
--------------------------------

健康状态的事实层是 `catalog_health`（只增不改）。注册表侧的"标记"用
**渠道标签**表达（`health-unhealthy`）：恢复健康后标签被移除，历史仍完整留在
`catalog_health` 里。

为什么不用 `channel.enabled=False` 当"失效标记"：

- `enabled=False` 的语义是"用户主动停用"，把它当成"源失效"会让两个不同的概念
  共用一个开关，用户一停用就再也看不出源是否还活着；
- 停用会真的停止采集，而 SPEC §2.4 精神是**只标注不改变条目身份**；
- 恢复后要"重新标记为健康"，如果标记落在 `enabled` 上，就会与用户的停用决定打架。

标签标记是可逆的、纯信息性的，且所有写入都经 `RegistryService.update_channel()`，
因此照样跑完整校验并留下版本与审计记录。**任何情况下都不删除渠道记录。**
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

from atlas.catalog.health import (
    ChannelHealth,
    HealthReport,
    HealthStatus,
)
from atlas.registry.schema import Channel
from atlas.registry.service import RegistryService
from atlas.registry.versioning import ConfigVersion

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_UNHEALTHY_TAG",
    "CatalogHealthStore",
    "open_health_store",
]

#: SPEC §2.10 的目录布局：健康状态与配置共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

#: 写回注册表时使用的标签名（自由标签字段，SPEC §2.9 `channel.tags`）。
DEFAULT_UNHEALTHY_TAG = "health-unhealthy"

_DDL = """
-- SPEC §2.10 表归属：catalog_health 属于 T-111（渠道健康状态；只增不改，最新一条为当前状态）
CREATE TABLE IF NOT EXISTS catalog_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    status          TEXT NOT NULL,
    healthy         INTEGER NOT NULL,
    checked_at      TEXT NOT NULL,
    reason          TEXT NOT NULL,
    http_status     INTEGER,
    robots_outcome  TEXT,
    robots_url      TEXT,
    waited_seconds  REAL NOT NULL DEFAULT 0.0,
    detail          TEXT
);

-- "某渠道的最新状态"与"最近的失效渠道"这两类查询各有一条索引。
CREATE INDEX IF NOT EXISTS idx_catalog_health_channel ON catalog_health(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_catalog_health_status  ON catalog_health(healthy, id);

-- SPEC §2.4 / §2.10 精神：健康历史只增不改，用触发器强制（不是靠调用方约定）
CREATE TRIGGER IF NOT EXISTS trg_catalog_health_no_update
BEFORE UPDATE ON catalog_health
BEGIN
    SELECT RAISE(ABORT, 'catalog_health is append-only: UPDATE is forbidden (SPEC 2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_catalog_health_no_delete
BEFORE DELETE ON catalog_health
BEGIN
    SELECT RAISE(ABORT, 'catalog_health is append-only: DELETE is forbidden (SPEC 2.10)');
END;
"""

_COLUMNS = (
    "id, channel_id, endpoint, status, healthy, checked_at, reason, "
    "http_status, robots_outcome, robots_url, waited_seconds, detail"
)

_PathLike = Union[str, Path]


class CatalogHealthStore:
    """`catalog_health` 的 SQLite 仓储：只插入与查询，没有 UPDATE / DELETE 的代码路径。"""

    def __init__(self, db_path: Optional[_PathLike] = None) -> None:
        self._path = DEFAULT_DB_PATH if db_path is None else Path(db_path)
        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（只读诊断与触发器测试用）。写入请走 `record()`。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CatalogHealthStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 写：只插入
    # ------------------------------------------------------------------
    def record(self, health: ChannelHealth) -> int:
        """追加一条探测结果，返回该行的 `id`（= 本次探测在历史里的位置）。"""
        cursor = self._conn.execute(
            """
            INSERT INTO catalog_health(
                channel_id, endpoint, status, healthy, checked_at, reason,
                http_status, robots_outcome, robots_url, waited_seconds, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                health.channel_id,
                health.endpoint,
                health.status.value,
                1 if health.healthy else 0,
                health.checked_at.astimezone(timezone.utc).isoformat(),
                health.reason,
                health.http_status,
                health.robots_outcome,
                health.robots_url,
                float(health.waited_seconds),
                health.detail,
            ),
        )
        return int(cursor.lastrowid or 0)

    def record_report(self, report: HealthReport) -> Tuple[int, ...]:
        """把一轮探测报告整体落盘（逐条追加，返回各行 id）。"""
        return tuple(self.record(result) for result in report.results)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM catalog_health").fetchone()
        return int(row["n"])

    def history(self, channel_id: str) -> Tuple[ChannelHealth, ...]:
        """某渠道的完整健康历史（按探测顺序升序）——只增不改，因此历史从不说谎。"""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM catalog_health WHERE channel_id = ? ORDER BY id",
            (channel_id,),
        ).fetchall()
        return tuple(_row_to_health(row) for row in rows)

    def latest(self, channel_id: str) -> Optional[ChannelHealth]:
        """某渠道的**当前**状态 = 最新一条。没有探测过则返回 `None`（不编造"健康"）。"""
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM catalog_health WHERE channel_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
        return None if row is None else _row_to_health(row)

    def latest_all(
        self, *, healthy: Optional[bool] = None
    ) -> Tuple[ChannelHealth, ...]:
        """每个渠道的最新一条（可按健康与否过滤），按渠道 id 升序。

        用 `id = MAX(id) GROUP BY channel_id` 取最新：每个渠道一条，绝不重复出现。
        """
        sql = (
            f"SELECT {_COLUMNS} FROM catalog_health WHERE id IN "
            "(SELECT MAX(id) FROM catalog_health GROUP BY channel_id)"
        )
        params: Tuple[object, ...] = ()
        if healthy is not None:
            sql += " AND healthy = ?"
            params = (1 if healthy else 0,)
        sql += " ORDER BY channel_id"
        rows = self._conn.execute(sql, params).fetchall()
        return tuple(_row_to_health(row) for row in rows)

    def unhealthy(self) -> Tuple[ChannelHealth, ...]:
        """当前不健康的渠道（最新一条为不健康）。"""
        return self.latest_all(healthy=False)

    def healthy(self) -> Tuple[ChannelHealth, ...]:
        return self.latest_all(healthy=True)

    # ------------------------------------------------------------------
    # 写回注册表：只加/去标签，绝不删渠道
    # ------------------------------------------------------------------
    def sync_registry(
        self,
        service: RegistryService,
        *,
        channels: Optional[Iterable[Channel]] = None,
        author: Optional[str] = None,
        note: Optional[str] = None,
        unhealthy_tag: str = DEFAULT_UNHEALTHY_TAG,
    ) -> Tuple[ConfigVersion, ...]:
        """把最新健康状态标记写回注册表（幂等）。

        - 当前不健康且没有标记 → 加上 `unhealthy_tag`
        - 当前健康（或从未探测过）却有标记 → 去掉标记（**恢复后重新标记为健康**）
        - 没有任何渠道需要改 → 返回空元组（明确的"无变化"，不产生空版本）

        每次渠道变更都经 `RegistryService.update_channel()`：完整校验 + 新版本 + 审计。
        **不删除任何记录**，也不改动 `enabled`（见模块文档里的理由）。
        """
        if not unhealthy_tag or not unhealthy_tag.strip():
            raise ValueError("unhealthy_tag 不得为空（否则标记无从表达）")

        wanted = {result.channel_id for result in self.latest_all(healthy=False)}
        targets = tuple(service.list_channels() if channels is None else channels)

        versions: List[ConfigVersion] = []
        for channel in targets:
            should_mark = channel.id in wanted
            has_mark = unhealthy_tag in channel.tags
            if should_mark == has_mark:
                continue
            tags = (
                tuple(sorted({*channel.tags, unhealthy_tag}))
                if should_mark
                else tuple(t for t in channel.tags if t != unhealthy_tag)
            )
            versions.append(
                service.update_channel(
                    channel.id,
                    tags=tags,
                    author=author,
                    note=note
                    or (
                        f"T-111 健康标记：{'标记为不健康' if should_mark else '恢复健康'}"
                    ),
                )
            )
        return tuple(versions)


def _row_to_health(row: sqlite3.Row) -> ChannelHealth:
    return ChannelHealth(
        channel_id=str(row["channel_id"]),
        endpoint=str(row["endpoint"]),
        status=HealthStatus(str(row["status"])),
        checked_at=datetime.fromisoformat(str(row["checked_at"])),
        reason=str(row["reason"]),
        http_status=None if row["http_status"] is None else int(row["http_status"]),
        robots_outcome=row["robots_outcome"],
        robots_url=row["robots_url"],
        waited_seconds=float(row["waited_seconds"]),
        detail=row["detail"],
    )


def open_health_store(db_path: Optional[_PathLike] = None) -> CatalogHealthStore:
    """便捷入口：打开（必要时创建）健康状态仓储。"""
    return CatalogHealthStore(db_path)
