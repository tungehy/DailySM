"""
宏观数据库 Skill —— 管理 macro_nbs_data 表（PostgreSQL）

表结构：
  macro_nbs_data(
    id           SERIAL PRIMARY KEY,
    indicator_key  VARCHAR(64)   -- 指标键名，如 manufacturing_pmi
    indicator_name VARCHAR(128)  -- 中文名称
    stat_code      VARCHAR(16)   -- 统计局时间码，如 202501MM
    stat_period    VARCHAR(32)   -- 显示文字，如 2025年1月
    stat_year      SMALLINT      -- 年
    stat_month     SMALLINT      -- 月（季度数据取首月，年度数据为 0）
    value          NUMERIC(16,4) -- 数值
    unit           VARCHAR(32)   -- 单位
    created_at     TIMESTAMP     -- 写入时间
    UNIQUE(indicator_key, stat_code)
  )
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Column, Index, Integer, MetaData, Numeric, SmallInteger,
    String, Table, Text, UniqueConstraint, insert, select, text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

metadata = MetaData()

macro_nbs_data = Table(
    "macro_nbs_data",
    metadata,
    Column("id",             Integer,      primary_key=True, autoincrement=True),
    Column("indicator_key",  String(64),   nullable=False),
    Column("indicator_name", String(128),  nullable=False, default=""),
    Column("stat_code",      String(16),   nullable=False),
    Column("stat_period",    String(32),   nullable=False, default=""),
    Column("stat_year",      SmallInteger, nullable=False, default=0),
    Column("stat_month",     SmallInteger, nullable=False, default=0),
    Column("value",          Numeric(16, 4)),
    Column("unit",           String(32),   nullable=False, default=""),
    Column("created_at",     Text,         nullable=False, default=""),
    UniqueConstraint("indicator_key", "stat_code", name="uq_nbs_key_code"),
)

Index("ix_nbs_key",  macro_nbs_data.c.indicator_key)
Index("ix_nbs_code", macro_nbs_data.c.stat_code)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_macro_table(engine) -> None:
    """建表（如果不存在）"""
    metadata.create_all(engine)
    logger.info("[macro_db] macro_nbs_data 表初始化完成")


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def upsert_nbs_records(engine, records: list[dict[str, Any]]) -> int:
    """
    批量写入/更新宏观指标数据。
    records 中每条必须包含：indicator_key, stat_code, value
    可选：indicator_name, stat_period, stat_year, stat_month, unit
    """
    if not records:
        return 0

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in records:
        rows.append({
            "indicator_key":  r["indicator_key"],
            "indicator_name": r.get("indicator_name", ""),
            "stat_code":      r["stat_code"],
            "stat_period":    r.get("stat_period", ""),
            "stat_year":      r.get("stat_year", 0),
            "stat_month":     r.get("stat_month", 0),
            "value":          r.get("value"),
            "unit":           r.get("unit", ""),
            "created_at":     now_str,
        })

    stmt = pg_insert(macro_nbs_data).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_nbs_key_code",
        set_={
            "value":          stmt.excluded.value,
            "indicator_name": stmt.excluded.indicator_name,
            "stat_period":    stmt.excluded.stat_period,
            "stat_year":      stmt.excluded.stat_year,
            "stat_month":     stmt.excluded.stat_month,
            "unit":           stmt.excluded.unit,
            "created_at":     stmt.excluded.created_at,
        },
    )
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return len(rows)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def query_nbs_latest(engine, indicator_key: str, n: int = 12) -> list[dict]:
    """返回某指标最近 n 期（按 stat_code 降序）"""
    stmt = (
        select(macro_nbs_data)
        .where(macro_nbs_data.c.indicator_key == indicator_key)
        .order_by(macro_nbs_data.c.stat_code.desc())
        .limit(n)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def query_nbs_all(engine, indicator_keys: list[str] | None = None) -> dict[str, list[dict]]:
    """
    返回多个指标的完整序列（按 stat_code 升序）。
    indicator_keys=None 表示返回所有指标。
    """
    stmt = select(macro_nbs_data).order_by(
        macro_nbs_data.c.indicator_key,
        macro_nbs_data.c.stat_code,
    )
    if indicator_keys:
        stmt = stmt.where(macro_nbs_data.c.indicator_key.in_(indicator_keys))

    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()

    result: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        key = d["indicator_key"]
        result.setdefault(key, []).append(d)
    return result


def query_nbs_snapshot(engine, n_periods: int = 3) -> dict[str, list[dict]]:
    """
    返回每个指标最近 n_periods 期的快照，用于构建 LLM Prompt。
    格式：{indicator_key: [最近3期记录, ...]}
    """
    # 用子查询取每个指标前 n_periods 期
    with engine.connect() as conn:
        sql = text("""
            SELECT t.*
            FROM macro_nbs_data t
            WHERE t.id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY indicator_key ORDER BY stat_code DESC
                    ) AS rn
                    FROM macro_nbs_data
                ) ranked WHERE rn <= :n
            )
            ORDER BY indicator_key, stat_code DESC
        """)
        rows = conn.execute(sql, {"n": n_periods}).mappings().all()

    result: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        key = d["indicator_key"]
        result.setdefault(key, []).append(d)
    return result
