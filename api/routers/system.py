"""System（系统）：Agent 状态、调度、配置读写、通知渠道"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import (
    CONFIG_PATH, SCHEDULES_PATH, get_engine, load_yaml, redact, to_jsonable,
)

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/config")
def get_config():
    """读取 config.yaml（敏感字段已脱敏）"""
    cfg = load_yaml(CONFIG_PATH)
    return {"config": to_jsonable(redact(cfg)), "path": str(CONFIG_PATH)}


class ConfigUpdate(BaseModel):
    updates: dict   # {"llm.ark.model": "xxx", "decision_agent.top_n": 8}


def _set_nested(d: dict, dotted: str, value):
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


# 允许通过 API 修改的顶层 key 白名单（防止误改 api_key 等敏感结构）
_EDITABLE_TOP = {
    "agents", "research", "chat_agent", "decision_agent", "macro_agent",
    "sector_heat", "notifications", "report", "llm", "embedding",
    "bilibili", "download", "transcription",
}


@router.put("/config")
def update_config(req: ConfigUpdate):
    """按 dotted path 修改 config.yaml 常用配置（白名单 + 不回写脱敏值）。

    使用 ruamel.yaml round-trip 模式读写，保留原文件中的注释与排版。
    """
    from ruamel.yaml import YAML

    yaml_rt = YAML(typ="rt")          # round-trip：保留注释/格式
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 10 ** 6           # 不自动换行长字符串
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml_rt.load(f)

    for key, value in req.updates.items():
        top = key.split(".")[0]
        if top not in _EDITABLE_TOP:
            raise HTTPException(status_code=400, detail=f"不允许修改配置段: {top}")
        if value == "***":     # 脱敏占位符，跳过
            continue
        _set_nested(cfg, key, value)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml_rt.dump(cfg, f)
    return {"ok": True, "updated": list(req.updates.keys())}


@router.get("/schedules")
def get_schedules():
    """读取 schedules.yaml 调度配置（含 cron、启用状态、说明）"""
    cfg = load_yaml(SCHEDULES_PATH)
    return {"schedules": to_jsonable(cfg), "path": str(SCHEDULES_PATH)}


@router.get("/agents")
def agent_status():
    """各 Agent 运行状态（最新分析时间 + confidence + status）"""
    from ..deps import AGENT_SPECS
    from skills.analysis_repo import load_latest

    engine = get_engine()
    out = []
    for name, atype, label in AGENT_SPECS:
        try:
            row = load_latest(engine, name, atype)
            out.append({
                "agent": name, "label": label,
                "enabled_in_schedule": None,
                "last_run":  str(row.get("created_at", "")) if row else None,
                "trade_date": str(row.get("trade_date", "")) if row else None,
                "confidence": to_jsonable(row.get("confidence")) if row else None,
                "status":    (row.get("status") if row else None) or "no_data",
            })
        except Exception as e:
            out.append({"agent": name, "label": label, "status": "error", "error": str(e)})
    return {"agents": out}


@router.get("/notifications")
def get_notifications():
    """通知渠道配置（webhook 已脱敏）"""
    cfg = load_yaml(CONFIG_PATH).get("notifications", {})
    return {"notifications": to_jsonable(redact(cfg, "notifications"))}
