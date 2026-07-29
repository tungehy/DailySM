"""
统一市场观点库 Skill（Market Intelligence — Opinion Layer）

设计目标：
  所有"外部研究观点"（视频UP主 / 券商研报 / 新闻舆情 / 社交媒体）
  抽象为同一种原子单位——「一条观点」，统一存入 market_opinions 表。
  以后新增任何信息源（新 Agent），只需往这张表写，无需再建新表。

一条观点的定义：
  某个来源（谁）在某个时间，对某个标的（行业/大盘/主题）表达了一个
  方向性判断（看多/看空/中性），带有理由、强度、置信度和时效窗口。

三层架构中的位置：
  Raw 层（按源异构）：news_raw / videos(SQLite)+md / research_raw(未来)
        │  各 Agent 抽取
        ▼
  Opinion 层（本模块，统一）：market_opinions
        │  各 Agent / DecisionAgent 聚合
        ▼
  Analysis 层：analysis_results

时效性管理：
  - horizon（short/mid/long）决定 valid_until（失效时间点）
  - 查询时按 confidence × decay^已过天数 计算有效权重（越旧越轻）
  - 生命周期：active → expired（过期批量标记）/ superseded（同源同标的被新观点替代）
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, MetaData,
    String, Table, Text, and_, delete, func, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)
metadata = MetaData()

# ---------------------------------------------------------------------------
# 时效性配置
# ---------------------------------------------------------------------------

# horizon → 有效期天数（valid_until = publish_time + N 天）
HORIZON_VALID_DAYS: dict[str, int] = {
    "short": 3,     # 日内/突发：博主每日观点、突发新闻
    "mid":   14,    # 周度：周度复盘
    "long":  90,    # 月度/季度：月度策略、券商研报
}

# horizon → 每日时间衰减系数（查询时 effective_weight = confidence × decay^days）
HORIZON_DECAY: dict[str, float] = {
    "short": 0.70,  # 快速衰减
    "mid":   0.95,
    "long":  0.99,  # 缓慢衰减
}

# 不同来源的基础可信度权重（聚合时可乘到 effective_weight 上）
SOURCE_RELIABILITY: dict[str, float] = {
    "research": 1.0,   # 券商研报最权威
    "news":     0.8,
    "video":    0.7,
    "social":   0.5,
}

_DEFAULT_HORIZON = "short"


# ---------------------------------------------------------------------------
# 表定义
# ---------------------------------------------------------------------------

market_opinions = Table(
    "market_opinions",
    metadata,
    Column("id",           Integer,      primary_key=True, autoincrement=True),
    # —— 来源溯源 ——
    Column("source_type",  String(16),   nullable=False),                 # video / research / news / social
    Column("source_name",  String(96),   nullable=False, default=""),     # UP主名 / 机构名 / 平台名
    Column("author",       String(96),   nullable=True),                  # 分析师 / UP主
    Column("raw_ref",      String(160),  nullable=True),                  # 原始素材引用：bvid / news_id / 研报url
    # —— 观点标的 ——
    Column("target_type",  String(16),   nullable=False, default="sector"),  # sector / market / theme / index
    Column("target_name",  String(96),   nullable=False),                    # "半导体" / "大盘" / "AI算力"
    # —— 观点内容 ——
    Column("direction",    String(16),   nullable=False, default="neutral"), # bullish / bearish / neutral
    Column("strength",     Float,        nullable=True),                     # 0~100 观点强度
    Column("confidence",   Float,        nullable=True),                     # 0~1 置信度
    Column("reason",       Text,         nullable=True),                     # 一句话理由
    Column("keywords",     JSONB,        nullable=True),                     # list[str]
    # —— 时效管理 ——
    Column("horizon",      String(16),   nullable=False, default=_DEFAULT_HORIZON),  # short / mid / long
    Column("publish_time", DateTime,     nullable=False),                    # 观点发布时间（时效起点）
    Column("valid_until",  DateTime,     nullable=False),                    # 失效时间点
    Column("status",       String(16),   nullable=False, default="active"),  # active / expired / superseded
    # —— 元信息 ——
    Column("trade_date",   String(10),   nullable=False),
    Column("dedup_key",    String(64),   nullable=False, unique=True),       # 去重键（幂等重跑）
    Column("extra",        JSONB,        nullable=True),
    Column("created_at",   DateTime,     nullable=False),
)

Index("ix_mo_source_type", market_opinions.c.source_type)
Index("ix_mo_target_name", market_opinions.c.target_name)
Index("ix_mo_trade_date",  market_opinions.c.trade_date)
Index("ix_mo_status",      market_opinions.c.status)
Index("ix_mo_valid_until", market_opinions.c.valid_until)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_market_opinion_table(engine) -> None:
    """建表（幂等）"""
    metadata.create_all(engine)
    logger.info("[market_opinion_db] market_opinions 表初始化完成")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def compute_valid_until(publish_time: datetime, horizon: str) -> datetime:
    """根据 horizon 推导失效时间点"""
    days = HORIZON_VALID_DAYS.get(horizon, HORIZON_VALID_DAYS[_DEFAULT_HORIZON])
    return publish_time + timedelta(days=days)


def compute_dedup_key(
    source_type: str,
    source_name: str,
    target_name: str,
    direction:   str,
    trade_date:  str,
    raw_ref:     str = "",
) -> str:
    """
    生成去重键：同一来源、同一素材、同一标的、同一方向、同一交易日 视为同一条观点。
    用于幂等重跑（ON CONFLICT DO UPDATE）。
    """
    raw = f"{source_type}|{source_name}|{raw_ref}|{target_name}|{direction}|{trade_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def _to_datetime(value: Any, fallback: datetime | None = None) -> datetime:
    """把 date/datetime/str 统一转成 datetime"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:len(fmt) + 2], fmt)
            except ValueError:
                continue
    return fallback or datetime.now()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def insert_opinions(engine, opinions: list[dict]) -> int:
    """
    批量写入观点，dedup_key 冲突则更新（ON CONFLICT DO UPDATE）。
    支持幂等重跑：重复分析同一天同一来源不会产生重复观点。

    每条 opinion 必填：source_type, target_name, direction, trade_date
    可选：source_name, author, raw_ref, target_type, strength, confidence,
          reason, keywords, horizon, publish_time, extra

    返回提交的观点条数（含更新）。
    """
    if not opinions:
        return 0

    now = datetime.now()
    rows = []
    for o in opinions:
        source_type = str(o.get("source_type", "")).strip()
        target_name = str(o.get("target_name", "")).strip()
        direction   = str(o.get("direction", "neutral")).strip() or "neutral"
        if not source_type or not target_name:
            continue

        source_name = str(o.get("source_name", ""))[:96]
        raw_ref     = str(o.get("raw_ref", "") or "")[:160]
        trade_date  = str(o.get("trade_date", ""))
        horizon     = o.get("horizon") or _DEFAULT_HORIZON
        publish_dt  = _to_datetime(o.get("publish_time"), fallback=_to_datetime(trade_date, now))
        valid_until = o.get("valid_until") or compute_valid_until(publish_dt, horizon)

        dedup_key = o.get("dedup_key") or compute_dedup_key(
            source_type, source_name, target_name, direction, trade_date, raw_ref
        )

        rows.append({
            "source_type":  source_type,
            "source_name":  source_name,
            "author":       (o.get("author") or None),
            "raw_ref":      raw_ref or None,
            "target_type":  o.get("target_type", "sector"),
            "target_name":  target_name[:96],
            "direction":    direction,
            "strength":     o.get("strength"),
            "confidence":   o.get("confidence"),
            "reason":       (str(o.get("reason", ""))[:500] or None),
            "keywords":     o.get("keywords"),
            "horizon":      horizon,
            "publish_time": publish_dt,
            "valid_until":  valid_until,
            "status":       o.get("status", "active"),
            "trade_date":   trade_date,
            "dedup_key":    dedup_key,
            "extra":        o.get("extra"),
            "created_at":   now,
        })

    if not rows:
        return 0

    stmt = pg_insert(market_opinions).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["dedup_key"],
        set_={
            "strength":    stmt.excluded.strength,
            "confidence":  stmt.excluded.confidence,
            "reason":      stmt.excluded.reason,
            "keywords":    stmt.excluded.keywords,
            "horizon":     stmt.excluded.horizon,
            "valid_until": stmt.excluded.valid_until,
            "status":      stmt.excluded.status,
            "extra":       stmt.excluded.extra,
        },
    )
    with engine.begin() as conn:
        conn.execute(stmt)
    logger.info("[market_opinion_db] 观点入库：%d 条（source=%s）",
                len(rows), rows[0]["source_type"])
    return len(rows)


# ---------------------------------------------------------------------------
# 生命周期管理
# ---------------------------------------------------------------------------

def expire_opinions(engine, as_of: datetime | None = None) -> int:
    """把已过 valid_until 的 active 观点批量标为 expired。返回影响条数。"""
    now = as_of or datetime.now()
    with engine.begin() as conn:
        result = conn.execute(
            update(market_opinions)
            .where(market_opinions.c.status == "active")
            .where(market_opinions.c.valid_until < now)
            .values(status="expired")
        )
    n = result.rowcount
    if n:
        logger.info("[market_opinion_db] 标记 %d 条观点为 expired", n)
    return n


def supersede_prior(
    engine,
    source_type: str,
    source_name: str,
    target_name: str,
    before_trade_date: str,
) -> int:
    """
    同一来源对同一标的出现新观点时，把该来源之前的 active 观点标为 superseded。
    仅作用于早于 before_trade_date 的记录。
    """
    with engine.begin() as conn:
        result = conn.execute(
            update(market_opinions)
            .where(market_opinions.c.source_type == source_type)
            .where(market_opinions.c.source_name == source_name)
            .where(market_opinions.c.target_name == target_name)
            .where(market_opinions.c.status == "active")
            .where(market_opinions.c.trade_date < before_trade_date)
            .values(status="superseded")
        )
    return result.rowcount


def delete_opinions_for_source_date(
    engine,
    source_type: str,
    trade_date:  str | date,
) -> int:
    """清除某来源某交易日的观点（幂等重跑前调用）。"""
    td = str(trade_date)
    with engine.begin() as conn:
        result = conn.execute(
            delete(market_opinions)
            .where(market_opinions.c.source_type == source_type)
            .where(market_opinions.c.trade_date == td)
        )
    cnt = result.rowcount
    if cnt:
        logger.info("[market_opinion_db] 清除 %d 条旧观点（%s / %s）", cnt, source_type, td)
    return cnt


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_active_opinions(
    engine,
    target_name:  str | None = None,
    target_type:  str | None = None,
    source_type:  str | None = None,
    trade_date:   str | date | None = None,
    as_of:        datetime | None = None,
    include_expired: bool = False,
    with_weight:  bool = True,
) -> list[dict]:
    """
    查询有效观点（默认只返回 status=active 且未过 valid_until）。

    可按标的 / 来源 / 交易日过滤。
    with_weight=True 时为每条观点附加 effective_weight（时间衰减 × 来源可信度 × 置信度）。

    返回 list[dict]，按 effective_weight 降序（若 with_weight）。
    """
    now = as_of or datetime.now()
    conds = []
    if not include_expired:
        conds.append(market_opinions.c.status == "active")
        conds.append(market_opinions.c.valid_until >= now)
    if target_name:
        conds.append(market_opinions.c.target_name == target_name)
    if target_type:
        conds.append(market_opinions.c.target_type == target_type)
    if source_type:
        conds.append(market_opinions.c.source_type == source_type)
    if trade_date:
        conds.append(market_opinions.c.trade_date == str(trade_date))

    stmt = select(market_opinions)
    if conds:
        stmt = stmt.where(and_(*conds))
    stmt = stmt.order_by(market_opinions.c.publish_time.desc())

    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings().all()]

    if with_weight:
        for r in rows:
            r["effective_weight"] = _effective_weight(r, now)
        rows.sort(key=lambda x: x.get("effective_weight", 0), reverse=True)
    return rows


def _effective_weight(opinion: dict, now: datetime) -> float:
    """
    计算观点当前有效权重：
        confidence × source_reliability × decay^已过天数
    """
    conf   = float(opinion.get("confidence") or 0.5)
    horizon = opinion.get("horizon") or _DEFAULT_HORIZON
    decay  = HORIZON_DECAY.get(horizon, HORIZON_DECAY[_DEFAULT_HORIZON])
    rel    = SOURCE_RELIABILITY.get(opinion.get("source_type", ""), 0.6)

    pub = opinion.get("publish_time")
    if isinstance(pub, datetime):
        days = max(0.0, (now - pub).total_seconds() / 86400.0)
    else:
        days = 0.0

    return round(conf * rel * (decay ** days), 4)


def aggregate_by_target(
    engine,
    target_type: str | None = "sector",
    as_of:       datetime | None = None,
    top_n:       int = 30,
) -> list[dict]:
    """
    汇总所有有效观点，按标的聚合出「当前市场观点」。

    每个标的输出：
      {
        "target_name":      标的名,
        "net_score":        -1~+1 净看多/看空,
        "direction":        bullish/bearish/neutral,
        "bullish_weight":   看多总权重,
        "bearish_weight":   看空总权重,
        "opinion_count":    观点条数,
        "sources":          涉及来源集合,
        "top_reason":       权重最高的一条理由,
      }

    供 DecisionAgent 消费，替代分散在各 Agent 的板块观点聚合逻辑。
    """
    now = as_of or datetime.now()
    opinions = get_active_opinions(
        engine, target_type=target_type, as_of=now, with_weight=True
    )

    agg: dict[str, dict] = {}
    for o in opinions:
        name = o["target_name"]
        w    = o.get("effective_weight", 0.0)
        bucket = agg.setdefault(name, {
            "target_name":    name,
            "bullish_weight": 0.0,
            "bearish_weight": 0.0,
            "opinion_count":  0,
            "sources":        set(),
            "_best_w":        -1.0,
            "top_reason":     "",
            "_latest_pt":     None,
        })
        direction = o.get("direction", "neutral")
        if direction == "bullish":
            bucket["bullish_weight"] += w
        elif direction == "bearish":
            bucket["bearish_weight"] += w
        bucket["opinion_count"] += 1
        bucket["sources"].add(o.get("source_type", ""))
        pt = o.get("publish_time")
        if pt and (bucket["_latest_pt"] is None or pt > bucket["_latest_pt"]):
            bucket["_latest_pt"] = pt
        if w > bucket["_best_w"] and o.get("reason"):
            bucket["_best_w"]    = w
            bucket["top_reason"] = o["reason"]

    results: list[dict] = []
    for name, b in agg.items():
        bw, ew = b["bullish_weight"], b["bearish_weight"]
        total  = bw + ew + 1e-9
        net    = (bw - ew) / total
        direction = "bullish" if net > 0.15 else ("bearish" if net < -0.15 else "neutral")
        latest_pt = b["_latest_pt"]
        results.append({
            "target_name":    name,
            "net_score":      round(net, 3),
            "direction":      direction,
            "bullish_weight": round(bw, 3),
            "bearish_weight": round(ew, 3),
            "opinion_count":  b["opinion_count"],
            "sources":        sorted(s for s in b["sources"] if s),
            "top_reason":     b["top_reason"],
            "latest_time":    latest_pt.strftime("%Y-%m-%d %H:%M") if hasattr(latest_pt, "strftime") else (str(latest_pt)[:16] if latest_pt else None),
        })

    results.sort(key=lambda x: abs(x["net_score"]) * x["opinion_count"], reverse=True)
    return results[:top_n]


def count_opinions(engine) -> dict[str, int]:
    """按 status 统计观点数量（运维/调试用）"""
    stmt = select(market_opinions.c.status, func.count().label("cnt")).group_by(
        market_opinions.c.status
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# 历史数据迁移：news_industry_mapping → market_opinions
# ---------------------------------------------------------------------------

def migrate_from_news_industry_mapping(engine) -> int:
    """
    将旧 news_industry_mapping 表的历史行业观点迁移到 market_opinions。

    幂等：基于 dedup_key 冲突跳过/更新，可重复运行。
    返回迁移条数。旧表保留（不删除），仅停止写入。
    """
    from sqlalchemy import text

    # 旧表可能不存在
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT m.id, m.news_id, m.trade_date, m.industry_name, m.sentiment, "
                "       m.impact_score, m.confidence, m.reason, m.created_at, "
                "       r.source AS news_source, r.platform AS news_platform, "
                "       r.publish_time AS news_publish "
                "FROM news_industry_mapping m "
                "LEFT JOIN news_raw r ON r.id = m.news_id"
            )).mappings().all()
    except Exception as e:
        logger.warning("[market_opinion_db] 迁移跳过（news_industry_mapping 不存在？）: %s", e)
        return 0

    opinions = []
    for m in rows:
        td = str(m["trade_date"])
        opinions.append({
            "source_type":  "news",
            "source_name":  m.get("news_platform") or m.get("news_source") or "news",
            "raw_ref":      str(m.get("news_id") or ""),
            "target_type":  "sector",
            "target_name":  m["industry_name"],
            "direction":    m.get("sentiment", "neutral"),
            "strength":     (float(m["impact_score"]) * 100) if m.get("impact_score") is not None else None,
            "confidence":   m.get("confidence"),
            "reason":       m.get("reason"),
            "horizon":      "short",
            "publish_time": m.get("news_publish") or m.get("created_at") or td,
            "trade_date":   td,
        })

    n = insert_opinions(engine, opinions)
    logger.info("[market_opinion_db] 历史新闻观点迁移完成：%d 条", n)
    return n
