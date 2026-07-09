"""
市场指数数据库层（market_index_daily）

存储以下指数的日线 OHLCV 数据：
  上证指数 / 深证成指 / 创业板指 / 科创50 / 恒生指数 / 恒生科技指数
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import (
    BigInteger, Column, Date, Index, Integer, MetaData,
    Numeric, String, Table, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

metadata = MetaData()

market_index_daily = Table(
    "market_index_daily",
    metadata,
    Column("id",          Integer,      primary_key=True, autoincrement=True),
    Column("index_code",  String(16),   nullable=False),
    Column("index_name",  String(64),   nullable=False, default=""),
    Column("trade_date",  Date,         nullable=False),
    Column("open",        Numeric(14, 4), nullable=True),
    Column("high",        Numeric(14, 4), nullable=True),
    Column("low",         Numeric(14, 4), nullable=True),
    Column("close",       Numeric(14, 4), nullable=False),
    Column("volume",      BigInteger,   nullable=True),
    Column("amount",      Numeric(22, 2), nullable=True),   # 港股有，A股为 None
    Column("change_pct",  Numeric(8, 4),  nullable=True),   # 涨跌幅（%）
    UniqueConstraint("index_code", "trade_date", name="uq_idx_code_date"),
)

Index("ix_mid_code",  market_index_daily.c.index_code)
Index("ix_mid_date",  market_index_daily.c.trade_date)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_index_table(engine) -> None:
    """建表（幂等）"""
    metadata.create_all(engine)
    logger.info("[index_db] market_index_daily 表初始化完成")


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def upsert_index_records(engine, records: list[dict[str, Any]]) -> int:
    """
    批量 upsert 指数日线记录。
    records 每条需包含：index_code, index_name, trade_date, close，
    可选：open, high, low, volume, amount, change_pct。
    返回写入条数。
    """
    if not records:
        return 0

    stmt = pg_insert(market_index_daily).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["index_code", "trade_date"],
        set_={
            "index_name": stmt.excluded.index_name,
            "open":       stmt.excluded.open,
            "high":       stmt.excluded.high,
            "low":        stmt.excluded.low,
            "close":      stmt.excluded.close,
            "volume":     stmt.excluded.volume,
            "amount":     stmt.excluded.amount,
            "change_pct": stmt.excluded.change_pct,
        },
    )
    with engine.begin() as conn:
        conn.execute(stmt)
    logger.debug("[index_db] upsert %d 条指数数据", len(records))
    return len(records)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def query_index_history(
    engine,
    index_code: str,
    start_date: date | None = None,
    end_date:   date | None = None,
) -> list[dict]:
    """查询指定指数的历史日线数据，按日期升序"""
    from sqlalchemy import select
    stmt = (
        select(market_index_daily)
        .where(market_index_daily.c.index_code == index_code)
        .order_by(market_index_daily.c.trade_date)
    )
    if start_date:
        stmt = stmt.where(market_index_daily.c.trade_date >= start_date)
    if end_date:
        stmt = stmt.where(market_index_daily.c.trade_date <= end_date)

    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def query_index_latest(engine, index_code: str, n: int = 60) -> list[dict]:
    """查询最近 n 条记录（降序）"""
    from sqlalchemy import select
    stmt = (
        select(market_index_daily)
        .where(market_index_daily.c.index_code == index_code)
        .order_by(market_index_daily.c.trade_date.desc())
        .limit(n)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def query_all_indices_latest_date(engine) -> dict[str, date]:
    """返回每个指数代码对应的最新数据日期（用于增量更新）"""
    from sqlalchemy import func, select
    stmt = (
        select(
            market_index_daily.c.index_code,
            func.max(market_index_daily.c.trade_date).label("latest"),
        )
        .group_by(market_index_daily.c.index_code)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return {row[0]: row[1] for row in rows}
