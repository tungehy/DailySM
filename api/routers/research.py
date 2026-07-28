"""Research（研究中心）：30 天热力图、市场观点、宏观/新闻摘要"""
from __future__ import annotations

from fastapi import APIRouter

from ..deps import get_engine, to_jsonable

router = APIRouter(prefix="/api/research", tags=["research"])


@router.get("/heatmap")
def heatmap(days: int = 30):
    """30 天行业热点热力图矩阵：{sectors, dates, matrix[sector][date]=heat}"""
    from skills.heat_skill import HeatSkill

    skill = HeatSkill()
    rows = skill.get_heat_scores(days)
    if not rows:
        return {"sectors": [], "dates": [], "matrix": []}

    dates = sorted({str(r["trade_date"]) for r in rows})
    # 板块按最新一天热度排序
    latest_date = max(str(r["trade_date"]) for r in rows)
    latest_rows = [r for r in rows if str(r["trade_date"]) == latest_date]
    sector_order = [
        r["sector_name"] for r in sorted(
            latest_rows, key=lambda r: float(r.get("heat_score") or 0), reverse=True
        )
    ]
    cell: dict[str, dict[str, float]] = {}
    for r in rows:
        cell.setdefault(r["sector_name"], {})[str(r["trade_date"])] = to_jsonable(r.get("heat_score"))

    matrix = []
    for s in sector_order:
        row = [cell.get(s, {}).get(d) for d in dates]
        matrix.append({"sector": s, "values": row})
    return {"sectors": sector_order, "dates": dates, "matrix": matrix}


@router.get("/opinions")
def market_opinions(target_type: str = "sector", top_n: int = 30):
    """市场观点聚合（视频/研报/新闻 统一观点）"""
    from skills.market_opinion_db import aggregate_by_target

    engine = get_engine()
    rows = aggregate_by_target(engine, target_type=target_type, top_n=top_n)
    return {"opinions": to_jsonable(rows)}


@router.get("/macro")
def macro_summary():
    """最新宏观分析摘要"""
    from skills.analysis_repo import load_latest

    engine = get_engine()
    row = load_latest(engine, "macro", "")
    return {
        "found": bool(row),
        "date": str(row.get("trade_date", "")) if row else None,
        "summary": (row.get("summary") or "") if row else "",
        "confidence": to_jsonable(row.get("confidence")) if row else None,
    }


@router.get("/news")
def news_summary():
    """最新新闻情绪摘要"""
    from skills.analysis_repo import load_latest

    engine = get_engine()
    row = load_latest(engine, "news", "")
    return {
        "found": bool(row),
        "date": str(row.get("trade_date", "")) if row else None,
        "summary": (row.get("summary") or "") if row else "",
        "confidence": to_jsonable(row.get("confidence")) if row else None,
    }


@router.get("/agents")
def agent_results():
    """各 Agent 最新分析结果（System 页 + 对话推理依据）"""
    from skills.analysis_repo import load_latest
    from ..deps import AGENT_SPECS

    engine = get_engine()
    out = []
    for name, atype, label in AGENT_SPECS:
        try:
            row = load_latest(engine, name, atype)
            out.append({
                "agent":      name,
                "label":      label,
                "found":      bool(row),
                "trade_date": str(row.get("trade_date", "")) if row else None,
                "created_at": str(row.get("created_at", "")) if row else None,
                "confidence": to_jsonable(row.get("confidence")) if row else None,
                "status":     row.get("status") if row else None,
                "summary_preview": (row.get("summary") or "")[:300] if row else "",
            })
        except Exception as e:
            out.append({"agent": name, "label": label, "found": False, "error": str(e)})
    return {"agents": out}
