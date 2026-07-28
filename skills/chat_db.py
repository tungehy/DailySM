"""
对话历史 Skill（chat_history）

存储对话决策 Agent 的多轮对话记录，支持跨会话回顾。
会话内记忆由 ChatAgent 在内存中维护，本表用于持久化与审计。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Index, Integer, MetaData, String, Table, Text, select,
)

logger = logging.getLogger(__name__)
metadata = MetaData()

chat_history = Table(
    "chat_history",
    metadata,
    Column("id",         Integer,    primary_key=True, autoincrement=True),
    Column("session_id", String(48), nullable=False),
    Column("role",       String(16), nullable=False),   # user / assistant
    Column("content",    Text,       nullable=False),
    Column("created_at", DateTime,   nullable=False),
)

Index("ix_ch_session", chat_history.c.session_id)
Index("ix_ch_created", chat_history.c.created_at)


def init_chat_table(engine) -> None:
    """建表（幂等）"""
    metadata.create_all(engine)
    logger.info("[chat_db] chat_history 表初始化完成")


def save_message(engine, session_id: str, role: str, content: str) -> None:
    """保存一条对话消息"""
    with engine.begin() as conn:
        conn.execute(chat_history.insert().values(
            session_id = session_id,
            role       = role,
            content    = content,
            created_at = datetime.now(),
        ))


def get_recent(engine, session_id: str, limit: int = 10) -> list[dict]:
    """读取指定会话最近 limit 条消息（按时间升序返回）"""
    stmt = (
        select(chat_history)
        .where(chat_history.c.session_id == session_id)
        .order_by(chat_history.c.created_at.desc())
        .limit(limit)
    )
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings().all()]
    return list(reversed(rows))
