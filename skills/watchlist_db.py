"""
自选关注 Skill（user_watchlist）

存储用户自选的 ETF / 行业 / 主题，供首页「我的关注」展示。
单用户系统：以 (watch_type, name) 唯一。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Index, Integer, MetaData, String, Table, delete, select,
)

logger = logging.getLogger(__name__)
metadata = MetaData()

user_watchlist = Table(
    "user_watchlist",
    metadata,
    Column("id",         Integer,    primary_key=True, autoincrement=True),
    Column("watch_type", String(16), nullable=False, default="sector"),  # sector/etf/theme
    Column("name",       String(96), nullable=False),
    Column("note",       String(255), nullable=True),
    Column("created_at", DateTime,   nullable=False),
)

Index("ix_uw_type", user_watchlist.c.watch_type)
Index("ix_uw_name", user_watchlist.c.name)


def init_watchlist_table(engine) -> None:
    metadata.create_all(engine)
    logger.info("[watchlist_db] user_watchlist 表初始化完成")


def add_watch(engine, name: str, watch_type: str = "sector", note: str | None = None) -> None:
    """添加关注（幂等，重复忽略）"""
    name = name.strip()
    if not name:
        return
    stmt = select(user_watchlist).where(
        user_watchlist.c.name == name, user_watchlist.c.watch_type == watch_type
    )
    with engine.begin() as conn:
        if conn.execute(stmt).first():
            return
        conn.execute(user_watchlist.insert().values(
            watch_type=watch_type, name=name, note=note, created_at=datetime.now()
        ))
    logger.info("[watchlist_db] 新增关注：%s (%s)", name, watch_type)


def list_watch(engine) -> list[dict]:
    stmt = select(user_watchlist).order_by(user_watchlist.c.created_at.desc())
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def remove_watch(engine, name: str, watch_type: str | None = None) -> int:
    cond = user_watchlist.c.name == name
    if watch_type:
        cond = cond & (user_watchlist.c.watch_type == watch_type)
    with engine.begin() as conn:
        r = conn.execute(delete(user_watchlist).where(cond))
    return r.rowcount
