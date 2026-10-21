"""轻量结构迁移。

项目没有引入 Alembic，而 Base.metadata.create_all() 只建表、不改表。
本模块只做「新增可空列 + 索引」这类向后兼容的变更，可重复执行。

规则：
  - 只加列，不删列、不改类型（避免误伤生产数据）；
  - 每步都是幂等的，容器每次启动都跑一遍也没副作用。
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger("claims.migrate")

# (表名, 列名, 列定义)
_ADD_COLUMNS: list[tuple[str, str, str]] = [
    # 账号：渠道 / 角色 / 显示名
    ("users", "channel", "VARCHAR(32) DEFAULT '' NOT NULL"),
    ("users", "role", "VARCHAR(16) DEFAULT 'editor' NOT NULL"),
    ("users", "display_name", "VARCHAR(64) DEFAULT '' NOT NULL"),
    # 事件：登记人 + 登记渠道（报表按账号维度的依据）
    ("claim_events", "created_by", "VARCHAR(64) DEFAULT '' NOT NULL"),
    ("claim_events", "created_channel", "VARCHAR(32) DEFAULT '' NOT NULL"),
]

_ADD_INDEXES: list[tuple[str, str, str]] = [
    ("ix_users_channel", "users", "channel"),
    ("ix_claim_events_created_by", "claim_events", "created_by"),
    ("ix_claim_events_created_channel", "claim_events", "created_channel"),
]


def ensure_schema(engine: Engine) -> None:
    """补齐缺失列与索引；已存在则跳过。"""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _ADD_COLUMNS:
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if column in existing:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            log.info("迁移：%s 新增列 %s", table, column)

        for index, table, column in _ADD_INDEXES:
            if table not in tables:
                continue
            existing_idx = {i["name"] for i in insp.get_indexes(table)}
            if index in existing_idx:
                continue
            try:
                conn.execute(text(f"CREATE INDEX {index} ON {table} ({column})"))
                log.info("迁移：%s 新增索引 %s", table, index)
            except Exception as exc:  # noqa: BLE001
                # 索引名冲突等不影响功能，降级为警告。
                log.warning("迁移：创建索引 %s 失败（可忽略）：%s", index, exc)

        # 老管理员账号补上 role=admin，避免升级后失去账号管理权限。
        # 注意：新增列时 Postgres 会按 DEFAULT 'editor' 填充老行，所以不能只判断空串。
        if "users" in tables:
            conn.execute(text(
                "UPDATE users SET role='admin' WHERE is_admin IS TRUE AND role <> 'admin'"
            ))
