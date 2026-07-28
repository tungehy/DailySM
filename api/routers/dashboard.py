"""Dashboard（首页）：市场指数、热门/超跌板块、今日 AI 投资决策"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel

from ..deps import get_engine, to_jsonable

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/indices")
def dashboard_indices(n: int = 20):
    """大盘指数卡片：当日指数 + 最近 N 日收盘走势"""
    from skills.index_db import query_index_latest
    from skills.index_skill import INDICES

    engine = get_engine()
    out = []
    for idx in INDICES:
        try:
            rows = query_index_latest(engine, idx["code"], n=n)   # 降序
            if not rows:
                continue
            rows_asc = list(reversed(rows))
            latest = rows[0]
            prev_close = rows[1]["close"] if len(rows) > 1 else None
            out.append({
                "code":        idx["code"],
                "name":        idx["name"],
                "market":      idx["market"],
                "close":       to_jsonable(latest["close"]),
                "change_pct":  to_jsonable(latest.get("change_pct")),
                "trade_date":  str(latest["trade_date"]),
                "history":     [
                    {"date": str(r["trade_date"]), "close": to_jsonable(r["close"])}
                    for r in rows_asc
                ],
            })
        except Exception:
            continue
    return {"indices": out}


@router.get("/sectors/hot")
def hot_sectors(n: int = 10, lookback: int = 20):
    """热门板块：热度排序 + 20 日热度走势"""
    from skills.heat_skill import HeatSkill

    skill = HeatSkill()
    latest = skill.get_latest_date()
    if not latest:
        return {"date": None, "sectors": []}
    rows = skill.get_heat_scores_by_date(latest)
    rows_sorted = sorted(rows, key=lambda r: float(r.get("heat_score") or 0), reverse=True)
    top = rows_sorted[:n]

    # 每个板块的近 lookback 日热度走势
    matrix = skill.get_heat_scores(lookback)
    hist_map: dict[str, list] = {}
    for r in matrix:
        hist_map.setdefault(r["sector_name"], []).append(
            {"date": str(r["trade_date"]), "heat": to_jsonable(r.get("heat_score"))}
        )
    for v in hist_map.values():
        v.sort(key=lambda x: x["date"])

    return {
        "date":    str(latest),
        "sectors": [
            {
                "sector":    r["sector_name"],
                "heat":      to_jsonable(r.get("heat_score")),
                "sort_score": to_jsonable(r.get("sort_score")),
                "continuous_capital_score": to_jsonable(r.get("continuous_capital_score")),
                "continuous_leader_score":  to_jsonable(r.get("continuous_leader_score")),
                "history":   hist_map.get(r["sector_name"], []),
            }
            for r in top
        ],
    }


@router.get("/sectors/oversold")
def oversold_sectors(n: int = 10):
    """超跌板块：按当日涨跌幅升序（跌幅最大）"""
    from skills.heat_skill import HeatSkill

    skill = HeatSkill()
    latest = skill.get_latest_date()
    if not latest:
        return {"date": None, "sectors": []}
    raw = skill.get_raw_by_date(latest)
    heat_map = {r["sector_name"]: r.get("heat_score") for r in skill.get_heat_scores_by_date(latest)}
    rows = [r for r in raw if r.get("price_change_pct") is not None]
    rows.sort(key=lambda r: float(r["price_change_pct"]))
    return {
        "date":    str(latest),
        "sectors": [
            {
                "sector":          r["sector_name"],
                "price_change_pct": to_jsonable(r.get("price_change_pct")),
                "heat":            to_jsonable(heat_map.get(r["sector_name"])),
                "turnover_amount": to_jsonable(r.get("turnover_amount")),
            }
            for r in rows[:n]
        ],
    }


@router.get("/sectors/oversold_period")
def oversold_period(days: int = 5, n: int = 10):
    """
    超跌板块（多周期）：按近 N 个交易日累计涨跌幅（复利）升序，跌幅最大在前。
    days ∈ {5, 20, 30, 60}
    """
    from datetime import timedelta
    from collections import defaultdict
    from skills.heat_skill import HeatSkill

    days = max(1, min(days, 120))
    skill = HeatSkill()
    latest = skill.get_latest_date()
    if not latest:
        return {"date": None, "days": days, "sectors": []}

    # 取最近 days 个交易日的原始数据（含当日）
    history = skill.get_raw_history(latest, days=days + 5)   # 含当日前的
    today_rows = skill.get_raw_by_date(latest)
    all_rows = list(history) + list(today_rows)

    # 按板块分组 -> 日期 -> pct
    by_sector: dict[str, dict[str, float]] = defaultdict(dict)
    for r in all_rows:
        if r.get("price_change_pct") is None:
            continue
        by_sector[r["sector_name"]][str(r["trade_date"])] = float(r["price_change_pct"])

    heat_map = {r["sector_name"]: r.get("heat_score") for r in skill.get_heat_scores_by_date(latest)}

    results = []
    for sector, datemap in by_sector.items():
        # 最近 days 个交易日（不晚于 latest）
        dates = sorted(d for d in datemap if d <= str(latest))[-days:]
        if len(dates) < max(2, days // 2):   # 数据不足则跳过
            continue
        cum = 1.0
        for d in dates:
            cum *= (1 + datemap[d] / 100.0)
        cum_pct = (cum - 1) * 100
        results.append({
            "sector": sector,
            "cum_change_pct": round(cum_pct, 2),
            "period_days": len(dates),
            "heat": to_jsonable(heat_map.get(sector)),
        })
    results.sort(key=lambda x: x["cum_change_pct"])
    return {"date": str(latest), "days": days, "sectors": results[:n]}


class WatchIn(BaseModel):
    name: str
    watch_type: str = "sector"
    note: str | None = None


@router.get("/watchlist")
def get_watchlist():
    """我的关注列表（附最新热度/涨跌幅）"""
    from skills.watchlist_db import init_watchlist_table, list_watch
    from skills.heat_skill import HeatSkill

    engine = get_engine()
    init_watchlist_table(engine)
    items = list_watch(engine)

    skill = HeatSkill()
    latest = skill.get_latest_date()
    heat_map, raw_map = {}, {}
    if latest:
        for r in skill.get_heat_scores_by_date(latest):
            heat_map[r["sector_name"]] = r.get("heat_score")
        for r in skill.get_raw_by_date(latest):
            raw_map[r["sector_name"]] = r.get("price_change_pct")

    return {
        "date": str(latest) if latest else None,
        "watchlist": [
            {
                **to_jsonable(i),
                "heat": to_jsonable(heat_map.get(i["name"])),
                "price_change_pct": to_jsonable(raw_map.get(i["name"])),
            }
            for i in items
        ],
    }


@router.post("/watchlist")
def add_watchlist(body: WatchIn):
    from skills.watchlist_db import add_watch, init_watchlist_table
    engine = get_engine()
    init_watchlist_table(engine)
    add_watch(engine, body.name, body.watch_type, body.note)
    return {"ok": True}


@router.delete("/watchlist/{name}")
def del_watchlist(name: str, watch_type: str | None = None):
    from skills.watchlist_db import init_watchlist_table, remove_watch
    engine = get_engine()
    init_watchlist_table(engine)
    n = remove_watch(engine, name, watch_type)
    return {"deleted": n}


@router.get("/report")
def today_report():
    """今日 AI 投资决策日报（decision agent 最新结果）"""
    from skills.analysis_repo import load_latest

    engine = get_engine()
    row = load_latest(engine, "decision", "decision")
    if not row:
        return {"found": False, "date": None, "summary": "", "raw": None}
    return {
        "found":   True,
        "date":    str(row.get("trade_date") or row.get("created_at", "")[:10]),
        "summary": row.get("summary") or "",
        "confidence": to_jsonable(row.get("confidence")),
        "created_at": str(row.get("created_at", "")),
        "raw":     to_jsonable(row.get("json_result")),
    }
