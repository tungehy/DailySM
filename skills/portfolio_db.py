"""
用户持仓 Skill（user_portfolio）

存储用户披露的持仓信息，供对话决策 Agent 生成个性化建议。
单用户系统：以持仓标的名（name）唯一，重复披露则更新（upsert）。

持仓可来自：
  - 对话中随口提到（ChatAgent 自动解析入库）
  - CLI 手动管理（python main.py portfolio ...）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, MetaData,
    String, Table, Text, delete, select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)
metadata = MetaData()

user_portfolio = Table(
    "user_portfolio",
    metadata,
    Column("id",           Integer,     primary_key=True, autoincrement=True),
    Column("holding_type", String(16),  nullable=False, default="sector"),  # sector/stock/etf/theme
    Column("name",         String(96),  nullable=False, unique=True),        # 标的名（唯一）
    Column("code",         String(32),  nullable=True),                      # 代码（可选）
    Column("cost_price",   Float,       nullable=True),                      # 成本价（可选）
    Column("position_pct", Float,       nullable=True),                      # 仓位占比 %（可选）
    Column("shares",       Float,       nullable=True),                      # 持股数（可选）
    Column("note",         Text,        nullable=True),                      # 备注
    Column("created_at",   DateTime,    nullable=False),
    Column("updated_at",   DateTime,    nullable=False),
)

Index("ix_up_type", user_portfolio.c.holding_type)


def init_portfolio_table(engine) -> None:
    """建表（幂等）"""
    metadata.create_all(engine)
    logger.info("[portfolio_db] user_portfolio 表初始化完成")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def upsert_holding(engine, holding: dict) -> None:
    """
    新增/更新一条持仓（按 name 唯一）。
    holding 必填：name；可选：holding_type, code, cost_price, position_pct, shares, note
    只更新本次显式提供的非 None 字段（避免对话中随口提一句就清空已有数据）。
    """
    name = str(holding.get("name", "")).strip()
    if not name:
        return
    now = datetime.now()

    values = {
        "holding_type": holding.get("holding_type") or "sector",
        "name":         name,
        "code":         holding.get("code"),
        "cost_price":   _num(holding.get("cost_price")),
        "position_pct": _num(holding.get("position_pct")),
        "shares":       _num(holding.get("shares")),
        "note":         holding.get("note"),
        "created_at":   now,
        "updated_at":   now,
    }
    # 冲突时只更新非 None 字段
    update_set = {
        k: v for k, v in values.items()
        if k not in ("name", "created_at") and v is not None
    }
    update_set["updated_at"] = now

    stmt = pg_insert(user_portfolio).values(values)
    stmt = stmt.on_conflict_do_update(index_elements=["name"], set_=update_set)
    with engine.begin() as conn:
        conn.execute(stmt)
    logger.info("[portfolio_db] 持仓更新：%s", name)


def get_holdings(engine) -> list[dict]:
    """返回全部持仓，按更新时间倒序"""
    stmt = select(user_portfolio).order_by(user_portfolio.c.updated_at.desc())
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def delete_holding(engine, name: str) -> int:
    """删除指定持仓"""
    with engine.begin() as conn:
        r = conn.execute(delete(user_portfolio).where(user_portfolio.c.name == name))
    return r.rowcount


def clear_holdings(engine) -> int:
    """清空全部持仓"""
    with engine.begin() as conn:
        r = conn.execute(delete(user_portfolio))
    return r.rowcount


def format_holdings(holdings: list[dict]) -> str:
    """格式化为便于 LLM 阅读的文本"""
    if not holdings:
        return "（用户暂未披露持仓）"
    lines = []
    for h in holdings:
        parts = [f"{h['name']}（{_type_cn(h.get('holding_type'))}"]
        if h.get("position_pct") is not None:
            parts.append(f"仓位{h['position_pct']:.0f}%")
        if h.get("cost_price") is not None:
            parts.append(f"成本{h['cost_price']:.2f}")
        if h.get("shares") is not None:
            parts.append(f"{h['shares']:.0f}股")
        seg = "，".join(parts) + "）"
        if h.get("note"):
            seg += f" 备注:{h['note']}"
        lines.append("- " + seg)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _type_cn(t: str | None) -> str:
    return {"sector": "行业", "stock": "个股", "etf": "ETF", "theme": "主题"}.get(t or "sector", "行业")
