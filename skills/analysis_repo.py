"""
分析结果仓库（Analysis Repository）

统一存储所有 Agent 的分析结果，支持：
  - 按 agent_name + analysis_type 去重保存（新记录自动将旧记录标为 superseded）
  - 有效期检查（expire_time 字段）
  - 加载最新有效结果
  - 从 JSON 恢复为 AgentResult 对象

表：analysis_results（PostgreSQL）
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Column, DateTime, Index, Integer, MetaData, Numeric, String,
    Table, Text, UniqueConstraint, select, text, update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

metadata = MetaData()

analysis_results = Table(
    "analysis_results",
    metadata,
    Column("id",            Integer,      primary_key=True, autoincrement=True),
    Column("agent_name",    String(32),   nullable=False),
    Column("analysis_type", String(64),   nullable=False, default=""),
    Column("data_version",  String(64),   nullable=False, default=""),
    Column("trade_date",    Text,         nullable=False),
    Column("summary",       Text,         nullable=False, default=""),
    Column("json_result",   Text,         nullable=False, default=""),
    Column("confidence",    Numeric(5, 4),nullable=True),
    Column("created_at",    DateTime,     nullable=False),
    Column("expire_time",   DateTime,     nullable=True),
    Column("status",        String(16),   nullable=False, default="active"),
)

Index("ix_ar_agent_type",  analysis_results.c.agent_name, analysis_results.c.analysis_type)
Index("ix_ar_status",      analysis_results.c.status)
Index("ix_ar_created",     analysis_results.c.created_at)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_analysis_table(engine) -> None:
    """建表（幂等）"""
    metadata.create_all(engine)
    logger.info("[analysis_repo] analysis_results 表初始化完成")


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

class _JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


def save_result(
    engine,
    result,                      # AgentResult
    analysis_type: str = "",
    expire_hours:  int = 24,
    data_version:  str = "",
) -> int:
    """
    保存 AgentResult 到 analysis_results。
    将同 agent_name + analysis_type 的旧 active 记录标为 superseded。
    返回新记录 id。
    """
    now        = datetime.now()
    expire_dt  = now + timedelta(hours=expire_hours)
    trade_str  = result.trade_date.isoformat() if hasattr(result.trade_date, "isoformat") else str(result.trade_date)
    json_str   = json.dumps(result.to_dict(), cls=_JsonEncoder, ensure_ascii=False)
    agent_name = result.agent_name
    atype      = analysis_type or ""
    dversion   = data_version or trade_str

    with engine.begin() as conn:
        # 将旧的 active 记录标为 superseded
        conn.execute(
            update(analysis_results)
            .where(analysis_results.c.agent_name    == agent_name)
            .where(analysis_results.c.analysis_type == atype)
            .where(analysis_results.c.status        == "active")
            .values(status="superseded")
        )
        # 插入新记录
        stmt = analysis_results.insert().values(
            agent_name    = agent_name,
            analysis_type = atype,
            data_version  = dversion,
            trade_date    = trade_str,
            summary       = result.summary[:4000],  # 截断过长 summary
            json_result   = json_str,
            confidence    = result.confidence,
            created_at    = now,
            expire_time   = expire_dt,
            status        = "active",
        )
        r = conn.execute(stmt)
        new_id = r.inserted_primary_key[0]

    logger.debug(
        "[analysis_repo] 保存 %s/%s id=%d expire=%s",
        agent_name, atype, new_id, expire_dt.strftime("%m-%d %H:%M"),
    )
    return new_id


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def load_latest(
    engine,
    agent_name:    str,
    analysis_type: str = "",
) -> dict | None:
    """
    加载最新 active 记录（不检查是否过期）。
    返回原始 row dict，或 None。
    """
    stmt = (
        select(analysis_results)
        .where(analysis_results.c.agent_name    == agent_name)
        .where(analysis_results.c.analysis_type == (analysis_type or ""))
        .where(analysis_results.c.status        == "active")
        .order_by(analysis_results.c.created_at.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return dict(rows[0]) if rows else None


def is_fresh(
    engine,
    agent_name:    str,
    analysis_type: str = "",
    max_age_hours: int = 24,
) -> bool:
    """
    检查是否有仍在有效期内的 active 分析结果。
    优先使用 expire_time 字段；若无则按 max_age_hours 计算。
    """
    row = load_latest(engine, agent_name, analysis_type)
    if row is None:
        return False
    now = datetime.now()
    if row.get("expire_time") and row["expire_time"] > now:
        return True
    # fallback: 按 created_at + max_age_hours
    created = row.get("created_at")
    if created and (now - created).total_seconds() < max_age_hours * 3600:
        return True
    return False


def load_if_fresh(
    engine,
    agent_name:    str,
    analysis_type: str = "",
    max_age_hours: int = 24,
) -> dict | None:
    """如果结果有效则返回，否则返回 None。"""
    if is_fresh(engine, agent_name, analysis_type, max_age_hours):
        return load_latest(engine, agent_name, analysis_type)
    return None


# ---------------------------------------------------------------------------
# AgentResult 反序列化
# ---------------------------------------------------------------------------

def dict_to_result(row: dict):
    """
    将 analysis_results 行的 json_result 字段反序列化为 AgentResult。
    """
    from agents.base import AgentResult
    data = json.loads(row["json_result"])
    return AgentResult(
        agent_name  = data["agent_name"],
        trade_date  = date.fromisoformat(data["trade_date"]),
        summary     = data.get("summary", ""),
        top_sectors = data.get("top_sectors", []),
        confidence  = float(data.get("confidence", 0)),
        mode        = data.get("mode", "cached"),
        raw_data    = data.get("raw_data", {}),
        timestamp   = datetime.fromisoformat(data["timestamp"]),
    )


def load_latest_as_result(engine, agent_name: str, analysis_type: str = ""):
    """加载最新结果并反序列化。如果没有则返回 None。"""
    row = load_latest(engine, agent_name, analysis_type)
    if row is None:
        return None
    try:
        return dict_to_result(row)
    except Exception as e:
        logger.warning("[analysis_repo] 反序列化失败 %s/%s: %s", agent_name, analysis_type, e)
        return None


# ---------------------------------------------------------------------------
# 清理过期记录（可定期调用）
# ---------------------------------------------------------------------------

def purge_old_records(engine, keep_days: int = 90) -> int:
    """删除 superseded 且 created_at 超过 keep_days 天的旧记录"""
    cutoff = datetime.now() - timedelta(days=keep_days)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM analysis_results "
                "WHERE status = 'superseded' AND created_at < :cutoff"
            ),
            {"cutoff": cutoff},
        )
    n = result.rowcount
    if n:
        logger.info("[analysis_repo] 清理 %d 条过期记录", n)
    return n
