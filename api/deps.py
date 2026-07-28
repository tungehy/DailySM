"""FastAPI 共享依赖与工具"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 引擎懒加载 + 线程安全
_engine = None
_engine_lock = threading.Lock()


def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from sector_heat.db import get_engine as _ge
                _engine = _ge()
    return _engine


def to_jsonable(v: Any) -> Any:
    """把 Decimal / datetime / date 等转为 JSON 可序列化"""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    return v


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH  = PROJECT_ROOT / "config" / "config.yaml"
SCHEDULES_PATH = PROJECT_ROOT / "config" / "schedules.yaml"

# 不应通过 API 回显的敏感字段（按路径前缀匹配）
SENSITIVE_KEYS = {
    "llm.*.api_key", "embedding.*.api_key",
    "sector_heat.database.password",
    "bilibili.sessdata", "download.cookies_file",
    "notifications.channels.dingtalk.webhook",
    "notifications.channels.dingtalk.secret",
    "notifications.channels.feishu.webhook",
    "notifications.channels.wecom.webhook",
    "database.password",
}


def load_yaml(path: Path) -> dict:
    import yaml
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def redact(cfg: Any, prefix: str = "") -> Any:
    """递归隐藏敏感字段"""
    if isinstance(cfg, dict):
        out = {}
        for k, v in cfg.items():
            path = f"{prefix}.{k}" if prefix else k
            if path in SENSITIVE_KEYS:
                out[k] = "***" if v else ""
            else:
                out[k] = redact(v, path)
        return out
    if isinstance(cfg, list):
        return [redact(x, prefix) for x in cfg]
    return cfg


# 各 Agent 的 analysis_type 对照
AGENT_SPECS = [
    ("heat",     "",     "板块热度"),
    ("quant",    "",     "量化分析"),
    ("macro",    "",     "宏观分析"),
    ("news",     "",     "新闻舆情"),
    ("research", "",     "研报观点"),
    ("video",    "video_group_a", "视频观点"),
    ("decision", "decision",     "投资决策"),
]
