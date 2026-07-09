"""
新闻数据库 Skill（Raw Data Layer + Feature Layer）

表：
  news_raw               — 原始新闻 + LLM 预处理结果（Raw Data Layer）
  news_industry_mapping  — 新闻行业映射（Feature Layer）

分层说明：
  NewsCrawler → news_raw（pending）
  NewsAgent   → news_industry_mapping + analysis_results，并将 news_raw 标为 completed
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, date
from typing import Any

from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, MetaData,
    String, Table, Text, delete, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)
metadata = MetaData()

# ─────────────────────────────────────────────────────────────────
# 表定义
# ─────────────────────────────────────────────────────────────────

news_raw = Table(
    "news_raw",
    metadata,
    Column("id",           Integer,     primary_key=True, autoincrement=True),
    Column("publish_time", DateTime,    nullable=True),
    Column("crawl_time",   DateTime,    nullable=False),
    Column("source",       String(64),  nullable=False, default=""),
    Column("platform",     String(64),  nullable=False, default=""),
    Column("title",        Text,        nullable=False),
    Column("url",          Text,        nullable=False, default=""),
    # LLM 预处理字段
    Column("summary",      Text,        nullable=True),
    Column("keywords",     JSONB,       nullable=True),   # list[str]
    Column("event_type",   String(64),  nullable=True),
    Column("entities",     JSONB,       nullable=True),   # list[str]
    # 管理字段
    Column("hash",         String(64),  nullable=False, unique=True),
    Column("status",       String(16),  nullable=False, default="pending"),
    Column("trade_date",   String(10),  nullable=False),
    Column("extra",        JSONB,       nullable=True),   # 扩展（流量分等）
    Column("created_at",   DateTime,    nullable=False),
)

news_industry_mapping = Table(
    "news_industry_mapping",
    metadata,
    Column("id",            Integer,    primary_key=True, autoincrement=True),
    Column("news_id",       Integer,    nullable=False),
    Column("trade_date",    String(10), nullable=False),
    Column("industry_name", String(64), nullable=False),
    Column("sentiment",     String(16), nullable=False, default="neutral"),
    Column("impact_score",  Float,      nullable=True),
    Column("confidence",    Float,      nullable=True),
    Column("reason",        Text,       nullable=True),
    Column("created_at",    DateTime,   nullable=False),
)

Index("ix_nr_hash",        news_raw.c.hash)
Index("ix_nr_trade_date",  news_raw.c.trade_date)
Index("ix_nr_status",      news_raw.c.status)
Index("ix_nim_news_id",    news_industry_mapping.c.news_id)
Index("ix_nim_trade_date", news_industry_mapping.c.trade_date)
Index("ix_nim_industry",   news_industry_mapping.c.industry_name)


# ─────────────────────────────────────────────────────────────────
# 初始化
# ─────────────────────────────────────────────────────────────────

def init_news_tables(engine) -> None:
    """建表（幂等）"""
    metadata.create_all(engine)
    logger.info("[news_db] news_raw + news_industry_mapping 表初始化完成")


# ─────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────

def compute_hash(title: str, source: str) -> str:
    """基于标题 + 来源平台生成去重 hash（SHA-256 前48位）"""
    raw = f"{source}::{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:48]


# ─────────────────────────────────────────────────────────────────
# news_raw CRUD
# ─────────────────────────────────────────────────────────────────

def insert_news_raw(engine, records: list[dict]) -> int:
    """
    批量插入新闻，hash 冲突则跳过（ON CONFLICT DO NOTHING）。
    返回实际插入条数。

    每条 record 必须包含：title, trade_date
    可选：source, platform, url, summary, keywords, event_type, entities, extra, publish_time
    """
    if not records:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now()
    rows = []
    for r in records:
        h = r.get("hash") or compute_hash(r["title"], r.get("source", ""))
        rows.append({
            "publish_time": r.get("publish_time"),
            "crawl_time":   r.get("crawl_time", now),
            "source":       r.get("source", ""),
            "platform":     r.get("platform", ""),
            "title":        r["title"],
            "url":          r.get("url", ""),
            "summary":      r.get("summary"),
            "keywords":     r.get("keywords"),
            "event_type":   r.get("event_type"),
            "entities":     r.get("entities"),
            "hash":         h,
            "status":       r.get("status", "pending"),
            "trade_date":   str(r.get("trade_date", "")),
            "extra":        r.get("extra"),
            "created_at":   now,
        })

    stmt = (
        pg_insert(news_raw)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["hash"])
    )
    with engine.begin() as conn:
        result = conn.execute(stmt)
    inserted = result.rowcount
    logger.info("[news_db] 新闻入库：实际 %d / 提交 %d（重复跳过）", inserted, len(rows))
    return inserted


def get_pending_news(engine, trade_date: str | date) -> list[dict]:
    """查询指定交易日 status=pending 的新闻，按 id 升序"""
    td = str(trade_date)
    stmt = (
        select(news_raw)
        .where(news_raw.c.trade_date == td)
        .where(news_raw.c.status == "pending")
        .order_by(news_raw.c.id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def get_all_news_for_date(engine, trade_date: str | date) -> list[dict]:
    """查询指定交易日所有新闻（含 pending / completed / failed）"""
    td = str(trade_date)
    stmt = (
        select(news_raw)
        .where(news_raw.c.trade_date == td)
        .order_by(news_raw.c.id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def count_news_for_date(engine, trade_date: str | date) -> dict[str, int]:
    """统计某天各状态新闻数量"""
    from sqlalchemy import func
    td = str(trade_date)
    stmt = (
        select(news_raw.c.status, func.count().label("cnt"))
        .where(news_raw.c.trade_date == td)
        .group_by(news_raw.c.status)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return {r[0]: r[1] for r in rows}


def mark_news_status(engine, news_ids: list[int], status: str) -> None:
    """批量更新新闻状态"""
    if not news_ids:
        return
    with engine.begin() as conn:
        conn.execute(
            update(news_raw)
            .where(news_raw.c.id.in_(news_ids))
            .values(status=status)
        )


def reset_news_to_pending(engine, trade_date: str | date) -> int:
    """将某天所有 completed/failed 的新闻重置为 pending（用于重新分析）"""
    td = str(trade_date)
    with engine.begin() as conn:
        result = conn.execute(
            update(news_raw)
            .where(news_raw.c.trade_date == td)
            .where(news_raw.c.status.in_(["completed", "failed"]))
            .values(status="pending")
        )
    return result.rowcount


# ─────────────────────────────────────────────────────────────────
# news_industry_mapping CRUD
# ─────────────────────────────────────────────────────────────────

def insert_industry_mappings(engine, mappings: list[dict]) -> int:
    """
    批量插入行业映射（直接插入，调用前应先清除当天旧数据）。

    每条 mapping 必须包含：news_id, trade_date, industry_name
    可选：sentiment, impact_score, confidence, reason
    """
    if not mappings:
        return 0
    now = datetime.now()
    rows = []
    for m in mappings:
        rows.append({
            "news_id":       int(m["news_id"]),
            "trade_date":    str(m["trade_date"]),
            "industry_name": m["industry_name"],
            "sentiment":     m.get("sentiment", "neutral"),
            "impact_score":  m.get("impact_score"),
            "confidence":    m.get("confidence"),
            "reason":        m.get("reason", ""),
            "created_at":    now,
        })
    with engine.begin() as conn:
        conn.execute(news_industry_mapping.insert(), rows)
    logger.info("[news_db] 行业映射入库：%d 条", len(rows))
    return len(rows)


def get_mappings_for_date(engine, trade_date: str | date) -> list[dict]:
    """查询指定交易日所有行业映射，按 impact_score 降序"""
    td = str(trade_date)
    stmt = (
        select(news_industry_mapping)
        .where(news_industry_mapping.c.trade_date == td)
        .order_by(news_industry_mapping.c.impact_score.desc())
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def delete_mappings_for_date(engine, trade_date: str | date) -> int:
    """清除某天的行业映射（重新分析时先清理）"""
    td = str(trade_date)
    with engine.begin() as conn:
        result = conn.execute(
            delete(news_industry_mapping)
            .where(news_industry_mapping.c.trade_date == td)
        )
    cnt = result.rowcount
    if cnt:
        logger.info("[news_db] 清除 %d 条旧行业映射（%s）", cnt, td)
    return cnt
