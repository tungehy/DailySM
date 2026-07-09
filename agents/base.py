"""
Agent 基础接口定义

AgentResult — 所有 Agent 的统一输出格式（JSON 可序列化）
BaseAgent   — 所有 Agent 的抽象基类，内置 Analysis Repository 读写支持
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """
    各 Agent 统一输出格式。

    Attributes:
        agent_name:   Agent 标识符，如 "heat" / "macro" / "news" / "video"
        trade_date:   分析对应的交易日
        summary:      LLM 或规则生成的板块观点文字摘要（Markdown）
        top_sectors:  板块推荐列表，每项格式：
                        {
                          "sector":    板块名,
                          "score":     0~100 综合得分,
                          "direction": "bullish" | "bearish" | "neutral",
                          "reason":    一句话理由
                        }
        confidence:   0~1，数据完整度/可信度
        mode:         数据来源或策略标识
        raw_data:     原始结构化数据（供 DecisionAgent 深度引用）
        timestamp:    生成时间
    """
    agent_name:  str
    trade_date:  date
    summary:     str
    top_sectors: list[dict[str, Any]]
    confidence:  float                = 1.0
    mode:        str                  = ""
    raw_data:    dict[str, Any]       = field(default_factory=dict)
    timestamp:   datetime             = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "agent_name":  self.agent_name,
            "trade_date":  self.trade_date.isoformat(),
            "summary":     self.summary,
            "top_sectors": self.top_sectors,
            "confidence":  self.confidence,
            "mode":        self.mode,
            "raw_data":    self.raw_data,
            "timestamp":   self.timestamp.isoformat(),
        }

    @property
    def bullish_sectors(self) -> list[dict]:
        return [s for s in self.top_sectors if s.get("direction") == "bullish"]

    @property
    def bearish_sectors(self) -> list[dict]:
        return [s for s in self.top_sectors if s.get("direction") == "bearish"]


class BaseAgent(ABC):
    """
    所有 Agent 的抽象基类。

    子类须实现：
        analyze(trade_date) -> AgentResult
        is_available()      -> bool
    """

    name:        str = "base"
    description: str = ""

    @abstractmethod
    def analyze(self, trade_date: date) -> AgentResult:
        """
        执行当日分析，返回标准 AgentResult。

        实现时若发生不可恢复错误，应捕获并返回包含错误信息的 AgentResult，
        而不是抛出异常（confidence=0 表示数据不可用）。
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """
        检查 Agent 所需数据源 / 依赖是否就绪，可用则返回 True。
        用于 DecisionAgent 跳过不可用的 Agent。
        """
        ...

    def _empty_result(self, trade_date: date, reason: str = "") -> AgentResult:
        """生成空结果（用于出错 fallback）"""
        return AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = f"[{self.name}] 数据不可用: {reason}",
            top_sectors = [],
            confidence  = 0.0,
            mode        = "unavailable",
        )

    # ------------------------------------------------------------------
    # Analysis Repository 便捷方法（所有子类均可使用）
    # ------------------------------------------------------------------

    def save_result(
        self,
        result:        "AgentResult",
        engine,
        analysis_type: str = "",
        expire_hours:  int = 24,
        data_version:  str = "",
    ) -> int:
        """
        将 AgentResult 保存到 analysis_results 表。
        返回新记录 id；失败时仅记录警告，不抛出异常。
        """
        try:
            from skills.analysis_repo import save_result as _save
            return _save(engine, result, analysis_type, expire_hours, data_version)
        except Exception as e:
            logger.warning("[%s] save_result 失败: %s", self.name, e)
            return -1

    @classmethod
    def load_latest_result(
        cls,
        engine,
        agent_name:    str,
        analysis_type: str = "",
    ) -> "AgentResult | None":
        """
        从 analysis_results 表加载最新 active 记录并反序列化。
        失败时返回 None，不抛出异常。
        """
        try:
            from skills.analysis_repo import load_latest_as_result
            return load_latest_as_result(engine, agent_name, analysis_type)
        except Exception as e:
            logger.warning("load_latest_result(%s/%s) 失败: %s", agent_name, analysis_type, e)
            return None

    @staticmethod
    def check_fresh(
        engine,
        agent_name:    str,
        analysis_type: str = "",
        max_age_hours: int = 24,
    ) -> bool:
        """
        检查 analysis_results 中是否有仍有效的结果。
        供 run-all 使用，决定是否跳过重新分析。
        """
        try:
            from skills.analysis_repo import is_fresh
            return is_fresh(engine, agent_name, analysis_type, max_age_hours)
        except Exception:
            return False
