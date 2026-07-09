"""
量化分析专用数据库 Skill

基于与 sector_heat/db.py 相同的 PostgreSQL 连接，
新增 sector_quant_index 表，存储四模型量化得分。

表结构：
  sector_quant_index
    trade_date              DATE
    sector_name             VARCHAR
    -- Model 1: 趋势动量
    momentum_5d             5日累计涨幅(%)
    momentum_10d            10日累计涨幅(%)
    momentum_20d            20日累计涨幅(%)
    momentum_score          动量综合得分 0-100
    -- Model 2: 成交额放量
    vol_ratio_20            今日/MA20成交额比值
    vol_direction_pct       今日涨跌幅（方向参考）
    vol_expansion_score     放量得分 0-100
    -- Model 3: 相对强弱
    rs_vs_bench_5d          5日超额收益(%)
    rs_vs_bench_20d         20日超额收益(%)
    rs_score                相对强弱得分 0-100
    -- Model 4: 趋势评分
    ma_alignment            MA多头排列程度 0-1
    dist_from_ma20          偏离MA20百分比(%)
    trend_score             趋势得分 0-100
    -- 综合
    quant_score             综合量化得分 0-100
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import yaml
from sqlalchemy import (
    Column, Date, Index, MetaData, Numeric,
    String, Table, create_engine, text,
)
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_YAML_PATH    = _PROJECT_ROOT / "config" / "config.yaml"

# ---------------------------------------------------------------------------
# 引擎复用（与 sector_heat/db.py 共享同一数据库连接）
# ---------------------------------------------------------------------------

_engine: Engine | None = None


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        with open(_YAML_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f).get("sector_heat", {}).get("database", {})
        host     = cfg.get("host",     "127.0.0.1")
        port     = cfg.get("port",     5432)
        dbname   = cfg.get("name",     "dailysm")   # key 为 name，与 sector_heat/db.py 一致
        user     = cfg.get("user",     "postgres")
        password = cfg.get("password", "")
        url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
        _engine = create_engine(url, pool_pre_ping=True, pool_size=3)
        logger.info("[QuantDB] 连接: %s:%s/%s", host, port, dbname)
    return _engine


@contextmanager
def _get_conn():
    with _get_engine().connect() as conn:
        yield conn
        conn.commit()


# ---------------------------------------------------------------------------
# 表定义
# ---------------------------------------------------------------------------

_metadata = MetaData()

sector_quant_index = Table(
    "sector_quant_index",
    _metadata,
    Column("trade_date",           Date,          nullable=False),
    Column("sector_name",          String(100),   nullable=False),
    # Model 1: 趋势动量
    Column("momentum_5d",          Numeric(8, 2)),
    Column("momentum_10d",         Numeric(8, 2)),
    Column("momentum_20d",         Numeric(8, 2)),
    Column("momentum_score",       Numeric(8, 2)),
    # Model 2: 成交额放量
    Column("vol_ratio_20",         Numeric(10, 4)),
    Column("vol_direction_pct",    Numeric(8, 2)),
    Column("vol_expansion_score",  Numeric(8, 2)),
    # Model 3: 相对强弱
    Column("rs_vs_bench_5d",       Numeric(8, 2)),
    Column("rs_vs_bench_20d",      Numeric(8, 2)),
    Column("rs_score",             Numeric(8, 2)),
    # Model 4: 趋势评分
    Column("ma_alignment",         Numeric(6, 4)),
    Column("dist_from_ma20",       Numeric(8, 2)),
    Column("trend_score",          Numeric(8, 2)),
    # 综合
    Column("quant_score",          Numeric(8, 2)),
)

_idx_quant_date = Index(
    "idx_sqi_date",
    sector_quant_index.c.trade_date,
)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_quant_table() -> None:
    """建表（若已存在则不重建）"""
    _metadata.create_all(_get_engine())
    logger.info("[QuantDB] sector_quant_index 表就绪")


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def upsert_quant_scores(rows: list[dict]) -> int:
    """
    插入或更新量化得分（冲突时以 (trade_date, sector_name) 为主键覆盖）。

    Args:
        rows: 与 sector_quant_index 列对应的字典列表

    Returns:
        写入行数
    """
    if not rows:
        return 0
    sql = text("""
        INSERT INTO sector_quant_index (
            trade_date, sector_name,
            momentum_5d, momentum_10d, momentum_20d, momentum_score,
            vol_ratio_20, vol_direction_pct, vol_expansion_score,
            rs_vs_bench_5d, rs_vs_bench_20d, rs_score,
            ma_alignment, dist_from_ma20, trend_score,
            quant_score
        ) VALUES (
            :trade_date, :sector_name,
            :momentum_5d, :momentum_10d, :momentum_20d, :momentum_score,
            :vol_ratio_20, :vol_direction_pct, :vol_expansion_score,
            :rs_vs_bench_5d, :rs_vs_bench_20d, :rs_score,
            :ma_alignment, :dist_from_ma20, :trend_score,
            :quant_score
        )
        ON CONFLICT (trade_date, sector_name)
        DO UPDATE SET
            momentum_5d          = EXCLUDED.momentum_5d,
            momentum_10d         = EXCLUDED.momentum_10d,
            momentum_20d         = EXCLUDED.momentum_20d,
            momentum_score       = EXCLUDED.momentum_score,
            vol_ratio_20         = EXCLUDED.vol_ratio_20,
            vol_direction_pct    = EXCLUDED.vol_direction_pct,
            vol_expansion_score  = EXCLUDED.vol_expansion_score,
            rs_vs_bench_5d       = EXCLUDED.rs_vs_bench_5d,
            rs_vs_bench_20d      = EXCLUDED.rs_vs_bench_20d,
            rs_score             = EXCLUDED.rs_score,
            ma_alignment         = EXCLUDED.ma_alignment,
            dist_from_ma20       = EXCLUDED.dist_from_ma20,
            trend_score          = EXCLUDED.trend_score,
            quant_score          = EXCLUDED.quant_score
    """)
    # 需要 PRIMARY KEY 约束，若建表时未添加则补充
    _ensure_pk()
    with _get_conn() as conn:
        conn.execute(sql, rows)
    logger.info("[QuantDB] upsert %d 行量化得分", len(rows))
    return len(rows)


def _ensure_pk():
    """确保 PRIMARY KEY 约束存在（建表后首次写入时调用）"""
    pk_sql = text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'sector_quant_index_pkey'
            ) THEN
                ALTER TABLE sector_quant_index
                ADD CONSTRAINT sector_quant_index_pkey
                PRIMARY KEY (trade_date, sector_name);
            END IF;
        END $$;
    """)
    try:
        with _get_conn() as conn:
            conn.execute(pk_sql)
    except Exception as e:
        logger.debug("[QuantDB] _ensure_pk: %s", e)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def query_quant_latest(trade_date: date) -> list[dict]:
    """
    查询指定日期的量化得分，按 quant_score 降序排列。

    Returns:
        [{"sector_name": ..., "quant_score": ..., "momentum_score": ..., ...}]
    """
    sql = text("""
        SELECT *
        FROM sector_quant_index
        WHERE trade_date = :trade_date
        ORDER BY quant_score DESC NULLS LAST
    """)
    with _get_conn() as conn:
        result = conn.execute(sql, {"trade_date": trade_date})
        return [dict(row._mapping) for row in result]


def query_quant_history(before_date: date, days: int = 20) -> list[dict]:
    """
    查询 before_date 之前最近 N 个交易日的量化得分历史。
    按 (trade_date ASC, sector_name) 排序。
    """
    sql = text("""
        WITH recent_dates AS (
            SELECT DISTINCT trade_date
            FROM sector_quant_index
            WHERE trade_date < :before_date
            ORDER BY trade_date DESC
            LIMIT :days
        )
        SELECT q.*
        FROM sector_quant_index q
        JOIN recent_dates d ON q.trade_date = d.trade_date
        ORDER BY q.trade_date ASC, q.sector_name
    """)
    with _get_conn() as conn:
        result = conn.execute(sql, {"before_date": before_date, "days": days})
        return [dict(row._mapping) for row in result]


def query_sector_price_volume_history(
    before_date: date,
    days: int = 80,
) -> list[dict]:
    """
    从 sector_daily_raw 查询 before_date 之前最近 N 个交易日的
    price_change_pct 和 turnover_amount（用于量化模型输入）。

    Returns:
        [{"trade_date":..., "sector_name":..., "price_change_pct":...,
          "turnover_amount":..., "main_net_inflow_pct":...}]
        按 trade_date ASC 排列
    """
    sql = text("""
        WITH recent_dates AS (
            SELECT DISTINCT trade_date
            FROM sector_daily_raw
            WHERE trade_date < :before_date
            ORDER BY trade_date DESC
            LIMIT :days
        )
        SELECT r.trade_date, r.sector_name,
               r.price_change_pct, r.turnover_amount,
               r.main_net_inflow_pct
        FROM sector_daily_raw r
        JOIN recent_dates d ON r.trade_date = d.trade_date
        ORDER BY r.trade_date ASC, r.sector_name
    """)
    # 使用 sector_heat/db.py 的同一连接
    from sector_heat.db import get_conn
    with get_conn() as conn:
        result = conn.execute(sql, {"before_date": before_date, "days": days})
        return [dict(row._mapping) for row in result]


def query_sector_price_volume_including(trade_date: date, days: int = 80) -> list[dict]:
    """
    查询包含 trade_date 在内的最近 N 个交易日的原始数据（用于当日计算）。
    等价于 query_sector_price_volume_history(trade_date + 1day, days)。
    """
    from datetime import timedelta
    return query_sector_price_volume_history(
        trade_date + timedelta(days=1), days
    )
