"""AI Assistant（对话）：对话接口 + 历史 + 推理依据"""
from __future__ import annotations

import threading
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import get_engine, to_jsonable

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

# 会话级 ChatAgent（保持多轮记忆）；session_id -> ChatAgent
_agents: dict[str, "object"] = {}
_agents_lock = threading.Lock()


def _get_agent(session_id: str):
    from agents.chat_agent import ChatAgent
    with _agents_lock:
        if session_id not in _agents:
            _agents[session_id] = ChatAgent(session_id=session_id)
        return _agents[session_id]


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@router.post("/chat")
def chat(req: ChatRequest):
    """对话：意图理解→按需补数据→融合生成；返回答案 + 会话 id + 推理依据"""
    session_id = req.session_id or datetime.now().strftime("%Y%m%d%H%M%S")
    agent = _get_agent(session_id)
    answer = agent.chat(req.message)
    return {
        "session_id": session_id,
        "answer":     answer,
        "industries": getattr(agent, "_last_industries", []),
    }


@router.get("/history")
def chat_history(session_id: str, limit: int = 50):
    """读取会话历史"""
    from skills.chat_db import get_recent, init_chat_table
    engine = get_engine()
    init_chat_table(engine)
    rows = get_recent(engine, session_id, limit=limit)
    return {"session_id": session_id, "messages": to_jsonable(rows)}


@router.get("/sessions")
def chat_sessions(limit: int = 20):
    """最近会话列表（用于切换/续聊）"""
    from sqlalchemy import func, select
    from skills.chat_db import chat_history, init_chat_table
    engine = get_engine()
    init_chat_table(engine)
    stmt = (
        select(
            chat_history.c.session_id,
            func.max(chat_history.c.created_at).label("last_at"),
            func.count().label("count"),
        )
        .group_by(chat_history.c.session_id)
        .order_by(func.max(chat_history.c.created_at).desc())
        .limit(limit)
    )
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings().all()]
    return {"sessions": to_jsonable(rows)}


@router.get("/reasoning")
def reasoning_basis(industry: str | None = None):
    """
    推理依据：某行业当前的数据支撑（热度/观点/各Agent摘要/研报）。
    无 industry 时返回全局各 Agent 摘要。
    """
    from skills.analysis_repo import load_latest
    from ..deps import AGENT_SPECS

    engine = get_engine()
    out: dict = {"industry": industry, "agents": [], "heat": None, "opinion": None, "research": []}

    for name, atype, label in AGENT_SPECS:
        try:
            row = load_latest(engine, name, atype)
            if row:
                out["agents"].append({
                    "agent": name, "label": label,
                    "trade_date": str(row.get("trade_date", "")),
                    "summary": (row.get("summary") or "")[:400],
                })
        except Exception:
            continue

    if industry:
        try:
            from skills.heat_skill import HeatSkill
            skill = HeatSkill()
            latest = skill.get_latest_date()
            if latest:
                rows = skill.get_heat_scores_by_date(latest)
                r = next((x for x in rows if x["sector_name"] == industry), None)
                if r:
                    out["heat"] = {
                        "trade_date": str(latest),
                        "heat_score": to_jsonable(r.get("heat_score")),
                        "sort_score": to_jsonable(r.get("sort_score")),
                    }
        except Exception:
            pass
        try:
            from skills.market_opinion_db import get_active_opinions
            ops = get_active_opinions(engine, target_name=industry, target_type="sector")
            out["opinion"] = {"count": len(ops), "items": to_jsonable(ops[:6])}
        except Exception:
            pass
        try:
            from agents.research_agent import ResearchAgent
            hits = ResearchAgent().retrieve(query=f"{industry}行业配置建议", industry=industry, top_k=3)
            out["research"] = to_jsonable(hits)
        except Exception:
            pass
    return out


@router.get("/portfolio")
def get_portfolio():
    """用户持仓"""
    from skills.portfolio_db import get_holdings, init_portfolio_table
    engine = get_engine()
    init_portfolio_table(engine)
    return {"holdings": to_jsonable(get_holdings(engine))}
