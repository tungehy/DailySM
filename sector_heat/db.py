"""
PostgreSQL 数据库层

建表、CRUD 操作。
使用 SQLAlchemy Core（不需要 ORM），psycopg2 作为驱动。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any
import logging

import yaml
from sqlalchemy import (
    create_engine, text, MetaData, Table, Column,
    BigInteger, Integer, Numeric, String, Date, TIMESTAMP,
    UniqueConstraint, Index,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_PATH  = _PROJECT_ROOT / "config" / "config.yaml"

# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def _load_db_cfg() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("sector_heat", {}).get("database", {})


def _build_dsn(cfg: dict) -> str:
    return (
        f"postgresql+psycopg2://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['name']}"
    )


# ---------------------------------------------------------------------------
# 引擎单例
# ---------------------------------------------------------------------------

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        cfg = _load_db_cfg()
        dsn = _build_dsn(cfg)
        _engine = create_engine(
            dsn,
            pool_size=cfg.get("pool_size", 5),
            max_overflow=cfg.get("max_overflow", 10),
            pool_pre_ping=True,
        )
        logger.info("PostgreSQL 引擎已创建: %s:%s/%s", cfg["host"], cfg["port"], cfg["name"])
    return _engine


@contextmanager
def get_conn():
    engine = get_engine()
    with engine.connect() as conn:
        yield conn


# ---------------------------------------------------------------------------
# 表定义
# ---------------------------------------------------------------------------

metadata = MetaData()

sector_daily_raw = Table(
    "sector_daily_raw", metadata,
    Column("id",                  BigInteger, primary_key=True, autoincrement=True),
    Column("trade_date",          Date,       nullable=False),
    Column("sector_name",         String(100),nullable=False),
    Column("price_change_pct",    Numeric(8, 4)),   # 涨跌幅 %
    Column("main_net_inflow",     Numeric(20, 2)),  # 主力净流入（万元）
    Column("main_net_inflow_pct", Numeric(8, 4)),   # 主力净流入净占比 %
    Column("turnover_amount",     Numeric(20, 2)),  # 成交额（万元）
    Column("rise_count",          Integer),         # 上涨家数
    Column("fall_count",          Integer),         # 下跌家数
    Column("limit_up_count",      Integer, default=0),  # 涨停家数
    Column("total_count",         Integer),         # 总家数
    Column("created_at",          TIMESTAMP(timezone=True), server_default=text("NOW()")),
    UniqueConstraint("trade_date", "sector_name", name="uq_sector_daily_raw"),
)

sector_heat_index = Table(
    "sector_heat_index", metadata,
    Column("id",               BigInteger, primary_key=True, autoincrement=True),
    Column("trade_date",       Date,       nullable=False),
    Column("sector_name",      String(100),nullable=False),
    # ---- 短期热度子分 ----
    Column("heat_score",       Numeric(6, 2)),  # 综合热度 0-100
    Column("price_score",      Numeric(6, 2)),
    Column("fund_score",       Numeric(6, 2)),
    Column("limit_up_score",   Numeric(6, 2)),
    Column("rise_ratio_score", Numeric(6, 2)),
    # ---- 长期动量子分 ----
    Column("daily_capital_score",      Numeric(6, 2)),   # 当日资金强度得分 0-100
    Column("daily_leader_score",       Numeric(6, 2)),   # 当日领涨得分 0-100
    Column("continuous_capital_score", Numeric(10, 4)),  # 累计资金流入分（衰减模型）
    Column("continuous_leader_score",  Numeric(10, 4)),  # 累计领涨分（衰减模型）
    # ---- 排序分（与颜色分离，仅用于热力图行业排序）----
    Column("sort_score",               Numeric(10, 4)),  # sort_score = prev×decay + heat_score
    Column("created_at",       TIMESTAMP(timezone=True), server_default=text("NOW()")),
    UniqueConstraint("trade_date", "sector_name", name="uq_sector_heat_index"),
)

# 查询常用索引
Index("idx_sdr_trade_date", sector_daily_raw.c.trade_date)
Index("idx_shi_trade_date", sector_heat_index.c.trade_date)


def init_db():
    """
    统一数据库初始化：创建所有表（幂等，不存在才建，已存在则跳过）。

    覆盖范围（Raw Data Layer）：
      sector_daily_raw       — 板块每日原始行情
      macro_nbs_data         — 国家统计局宏观指标
      news_raw               — 新闻原始数据 + LLM 预处理结果

    覆盖范围（Feature Layer）：
      sector_heat_index      — 板块热度指数
      sector_quant_index     — 量化模型得分
      news_industry_mapping  — 新闻行业映射（旧表，已由 market_opinions 替代，保留历史）

    覆盖范围（Opinion Layer）：
      market_opinions        — 统一市场观点库（视频/研报/新闻/社交外部观点）
      research_reports       — 研报原文 + 元数据（RAG Raw 层）
      research_chunks        — 研报切块 + 向量（RAG Vector 层，时效加权检索）

    覆盖范围（Analysis Layer）：
      analysis_results       — Agent 分析结果仓库

    其他：
      market_index_daily     — 市场指数日线（上证/深证/创业板/科创50/恒生/恒生科技）
      user_portfolio         — 用户持仓（对话决策 Agent）
      chat_history           — 对话历史（对话决策 Agent）
    """
    engine = get_engine()

    # ── Raw Data Layer ────────────────────────────────────────────────
    # 1. sector_heat 核心表（本文件定义）
    metadata.create_all(engine)

    # 2. 宏观数据表
    try:
        from skills.macro_db import init_macro_table
        init_macro_table(engine)
    except Exception as e:
        logger.warning("init_macro_table 失败: %s", e)

    # 3. 新闻原始数据表 + 行业映射表
    try:
        from skills.news_db import init_news_tables
        init_news_tables(engine)
    except Exception as e:
        logger.warning("init_news_tables 失败: %s", e)

    # ── Feature Layer ─────────────────────────────────────────────────
    # 4. 量化分析表（内部自建引擎）
    try:
        from skills.quant_db import init_quant_table
        init_quant_table()
    except Exception as e:
        logger.warning("init_quant_table 失败: %s", e)

    # ── Opinion Layer ─────────────────────────────────────────────────
    # 5. 统一市场观点库（视频/研报/新闻/社交观点统一归宿）
    try:
        from skills.market_opinion_db import init_market_opinion_table
        init_market_opinion_table(engine)
    except Exception as e:
        logger.warning("init_market_opinion_table 失败: %s", e)

    # 5.1 研报 RAG 知识库（原文 + 向量切块，时效加权检索）
    try:
        from skills.research_db import init_research_tables
        init_research_tables(engine)
    except Exception as e:
        logger.warning("init_research_tables 失败: %s", e)

    # ── Analysis Layer ────────────────────────────────────────────────
    # 6. Agent 分析结果仓库
    try:
        from skills.analysis_repo import init_analysis_table
        init_analysis_table(engine)
    except Exception as e:
        logger.warning("init_analysis_table 失败: %s", e)

    # ── 其他 ──────────────────────────────────────────────────────────
    # 7. 市场指数日线
    try:
        from skills.index_db import init_index_table
        init_index_table(engine)
    except Exception as e:
        logger.warning("init_index_table 失败: %s", e)

    # 8. 用户持仓 + 对话历史（对话决策 Agent）
    try:
        from skills.portfolio_db import init_portfolio_table
        init_portfolio_table(engine)
    except Exception as e:
        logger.warning("init_portfolio_table 失败: %s", e)
    try:
        from skills.chat_db import init_chat_table
        init_chat_table(engine)
    except Exception as e:
        logger.warning("init_chat_table 失败: %s", e)
    try:
        from skills.watchlist_db import init_watchlist_table
        init_watchlist_table(engine)
    except Exception as e:
        logger.warning("init_watchlist_table 失败: %s", e)

    logger.info("数据库表初始化完成（所有表）")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def upsert_daily_raw(rows: list[dict]) -> int:
    """
    批量写入原始指标，已存在的行按 ON CONFLICT DO UPDATE。
    rows: [{"trade_date": date, "sector_name": str, ...}, ...]
    返回写入行数。
    """
    if not rows:
        return 0

    upsert_sql = text("""
        INSERT INTO sector_daily_raw
            (trade_date, sector_name, price_change_pct, main_net_inflow,
             main_net_inflow_pct, turnover_amount, rise_count, fall_count,
             limit_up_count, total_count)
        VALUES
            (:trade_date, :sector_name, :price_change_pct, :main_net_inflow,
             :main_net_inflow_pct, :turnover_amount, :rise_count, :fall_count,
             :limit_up_count, :total_count)
        ON CONFLICT (trade_date, sector_name) DO UPDATE SET
            price_change_pct    = EXCLUDED.price_change_pct,
            main_net_inflow     = EXCLUDED.main_net_inflow,
            main_net_inflow_pct = EXCLUDED.main_net_inflow_pct,
            turnover_amount     = EXCLUDED.turnover_amount,
            rise_count          = EXCLUDED.rise_count,
            fall_count          = EXCLUDED.fall_count,
            limit_up_count      = EXCLUDED.limit_up_count,
            total_count         = EXCLUDED.total_count
    """)

    with get_conn() as conn:
        conn.execute(upsert_sql, rows)
        conn.commit()

    logger.info("upsert_daily_raw: %d 行", len(rows))
    return len(rows)


def upsert_heat_index(rows: list[dict]) -> int:
    """批量写入热度指数（含长期动量子分）"""
    if not rows:
        return 0

    upsert_sql = text("""
        INSERT INTO sector_heat_index
            (trade_date, sector_name, heat_score, price_score, fund_score,
             limit_up_score, rise_ratio_score,
             daily_capital_score, daily_leader_score,
             continuous_capital_score, continuous_leader_score,
             sort_score)
        VALUES
            (:trade_date, :sector_name, :heat_score, :price_score, :fund_score,
             :limit_up_score, :rise_ratio_score,
             :daily_capital_score, :daily_leader_score,
             :continuous_capital_score, :continuous_leader_score,
             :sort_score)
        ON CONFLICT (trade_date, sector_name) DO UPDATE SET
            heat_score                = EXCLUDED.heat_score,
            price_score               = EXCLUDED.price_score,
            fund_score                = EXCLUDED.fund_score,
            limit_up_score            = EXCLUDED.limit_up_score,
            rise_ratio_score          = EXCLUDED.rise_ratio_score,
            daily_capital_score       = EXCLUDED.daily_capital_score,
            daily_leader_score        = EXCLUDED.daily_leader_score,
            continuous_capital_score  = EXCLUDED.continuous_capital_score,
            continuous_leader_score   = EXCLUDED.continuous_leader_score,
            sort_score                = EXCLUDED.sort_score
    """)

    with get_conn() as conn:
        conn.execute(upsert_sql, rows)
        conn.commit()

    logger.info("upsert_heat_index: %d 行", len(rows))
    return len(rows)


def query_heat_matrix(lookback_days: int = 20) -> list[dict]:
    """
    查询最近 N 个有数据的交易日的热度矩阵。
    返回 [{"trade_date": date, "sector_name": str, "heat_score": float, ...}, ...]
    """
    sql = text("""
        WITH recent_dates AS (
            SELECT DISTINCT trade_date
            FROM sector_heat_index
            ORDER BY trade_date DESC
            LIMIT :lookback
        )
        SELECT h.trade_date, h.sector_name, h.heat_score,
               h.price_score, h.fund_score,
               h.limit_up_score, h.rise_ratio_score,
               h.daily_capital_score, h.daily_leader_score,
               h.continuous_capital_score, h.continuous_leader_score,
               h.sort_score
        FROM sector_heat_index h
        JOIN recent_dates d ON h.trade_date = d.trade_date
        ORDER BY h.trade_date, h.sector_name
    """)
    with get_conn() as conn:
        result = conn.execute(sql, {"lookback": lookback_days})
        return [dict(row._mapping) for row in result]


def query_prev_continuous_scores(trade_date: date) -> dict[str, dict]:
    """
    查询 trade_date 之前最近一个交易日的连续得分。
    返回 {sector_name: {"continuous_capital_score": float, "continuous_leader_score": float}}
    首次无历史数据时返回空字典（视为 0 初始化）。
    """
    sql = text("""
        SELECT sector_name, continuous_capital_score, continuous_leader_score
        FROM sector_heat_index
        WHERE trade_date = (
            SELECT MAX(trade_date)
            FROM sector_heat_index
            WHERE trade_date < :trade_date
        )
    """)
    with get_conn() as conn:
        result = conn.execute(sql, {"trade_date": trade_date})
        rows = result.fetchall()

    return {
        row[0]: {
            "continuous_capital_score": float(row[1]) if row[1] is not None else 0.0,
            "continuous_leader_score":  float(row[2]) if row[2] is not None else 0.0,
        }
        for row in rows
    }


def query_prev_sort_scores(trade_date: date) -> dict[str, float]:
    """
    查询 trade_date 之前最近一个交易日的 sort_score。
    返回 {sector_name: sort_score}，无历史时返回空字典（首日用 heat_score 初始化）。
    """
    sql = text("""
        SELECT sector_name, sort_score
        FROM sector_heat_index
        WHERE trade_date = (
            SELECT MAX(trade_date)
            FROM sector_heat_index
            WHERE trade_date < :trade_date
        )
    """)
    with get_conn() as conn:
        result = conn.execute(sql, {"trade_date": trade_date})
        rows = result.fetchall()

    return {
        row[0]: float(row[1]) if row[1] is not None else 0.0
        for row in rows
    }


def query_raw_by_date(trade_date: date) -> list[dict]:
    """查询指定交易日的原始指标"""
    sql = text("""
        SELECT * FROM sector_daily_raw
        WHERE trade_date = :trade_date
        ORDER BY sector_name
    """)
    with get_conn() as conn:
        result = conn.execute(sql, {"trade_date": trade_date})
        return [dict(row._mapping) for row in result]


def get_latest_trade_date() -> date | None:
    """获取数据库中最新的交易日"""
    sql = text("SELECT MAX(trade_date) FROM sector_daily_raw")
    with get_conn() as conn:
        row = conn.execute(sql).fetchone()
    return row[0] if row else None


def query_raw_history(before_date: date, days: int = 130) -> list[dict]:
    """
    查询 before_date 之前最近 N 个交易日的原始数据，用于 HistoryModeStrategy 的滚动计算。

    Args:
        before_date: 截止日期（不含，即 < before_date）
        days:        最多返回的交易日数量（默认 130，能覆盖 vol_pct_period=120）

    Returns:
        [{"trade_date": date, "sector_name": str, "price_change_pct": ...,
          "turnover_amount": ..., ...}, ...]，按 trade_date 升序排列
    """
    sql = text("""
        WITH recent_dates AS (
            SELECT DISTINCT trade_date
            FROM sector_daily_raw
            WHERE trade_date < :before_date
            ORDER BY trade_date DESC
            LIMIT :days
        )
        SELECT r.trade_date, r.sector_name, r.price_change_pct,
               r.main_net_inflow, r.main_net_inflow_pct,
               r.turnover_amount, r.rise_count, r.fall_count,
               r.limit_up_count, r.total_count
        FROM sector_daily_raw r
        JOIN recent_dates d ON r.trade_date = d.trade_date
        ORDER BY r.trade_date ASC, r.sector_name
    """)
    with get_conn() as conn:
        result = conn.execute(sql, {"before_date": before_date, "days": days})
        return [dict(row._mapping) for row in result]
