"""
行业板块投资决策 Agent（总编排）

汇总所有子 Agent 的 AgentResult，通过加权聚合 + LLM 综合分析，
生成最终的板块投资决策报告（JSON + Markdown）。

工作流：
  1. 聚合各 Agent 的 top_sectors（加权投票）
  2. LLM 综合分析（消费所有 Agent 的 summary + raw_data）
  3. 输出最终 AgentResult + 保存 Markdown 报告
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agents.base import AgentResult, BaseAgent
from skills.llm_client import LLMClient

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_REPORT_DIR   = _PROJECT_ROOT / "data" / "reports"


# ---------------------------------------------------------------------------
# Agent 权重配置（可在 config.yaml 中覆盖）
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    "heat":  1.2,   # 量化热度，数据最直接
    "macro": 1.0,   # 宏观配置，中长期视角
    "news":  0.9,   # 舆情驱动，短期催化
    "video": 0.8,   # 视频观点，主观但有效
    "quant": 0.5,   # 量化（暂未实现）
}


class DecisionAgent(BaseAgent):
    """
    行业板块投资决策 Agent（总编排）

    参数：
        agent_weights:    各 Agent 权重字典（覆盖 DEFAULT_WEIGHTS）
        top_n:            最终推荐板块数量
        save_report:      是否保存 Markdown 报告到 data/reports/
    """

    name        = "decision"
    description = "板块投资决策总编排（汇总所有子Agent + LLM综合分析）"

    def __init__(
        self,
        agent_weights: dict[str, float] | None = None,
        top_n: int = 10,
        save_report: bool = True,
    ):
        self.weights     = {**DEFAULT_WEIGHTS, **(agent_weights or {})}
        self.top_n       = top_n
        self.save_report = save_report
        self._llm        = LLMClient()

    def is_available(self) -> bool:
        return True  # 决策 Agent 始终可用（兜底规则引擎）

    def analyze(self, trade_date: date) -> AgentResult:
        """单独运行时返回空结果（应通过 aggregate 调用）"""
        return self._empty_result(trade_date, "请通过 aggregate(results) 调用决策 Agent")

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def aggregate(
        self,
        results: list[AgentResult],
        trade_date: date | None = None,
    ) -> AgentResult:
        """
        汇总所有子 Agent 结果，生成最终决策报告。

        Args:
            results:     各子 Agent 的 AgentResult 列表
            trade_date:  交易日（默认从 results 推断）

        Returns:
            最终 AgentResult（agent_name="decision"）
        """
        if not results:
            return self._empty_result(trade_date or date.today(), "无子 Agent 结果")

        trade_date = trade_date or results[0].trade_date
        available  = [r for r in results if r.confidence > 0]
        logger.info(
            "[DecisionAgent] 聚合 %d/%d 个有效 Agent 结果: %s",
            len(available), len(results),
            [r.agent_name for r in available],
        )

        if not available:
            return self._empty_result(trade_date, "所有子 Agent 均无有效数据")

        # 1. 加权聚合板块得分
        aggregated = self._aggregate_sectors(available)

        # 2. LLM 综合分析
        final_report = self._llm_synthesis(trade_date, available, aggregated)

        # 3. 构造 top_sectors
        top_sectors = [
            {
                "sector":    s["sector"],
                "score":     round(s["weighted_score"], 1),
                "direction": s["direction"],
                "reason":    s["reason"],
                "agents":    s["contributing_agents"],
            }
            for s in aggregated[: self.top_n]
        ]

        # 4. 构造摘要
        hot_names  = "、".join(
            s["sector"] for s in top_sectors[:5] if s["direction"] == "bullish"
        ) or "暂无"
        cold_names = "、".join(
            s["sector"] for s in top_sectors if s["direction"] == "bearish"
        )[:3] or "无"
        agent_list = "、".join(r.agent_name for r in available)
        summary = (
            f"## 行业板块投资决策日报（{trade_date}）\n\n"
            f"**数据来源**：{agent_list}\n\n"
            f"**综合看多板块**：{hot_names}\n\n"
            f"**综合看空板块**：{cold_names}\n\n"
            f"---\n\n{final_report}"
        )

        # 5. 保存报告
        if self.save_report:
            self._save_report(trade_date, summary, top_sectors, results)

        avg_conf = sum(r.confidence for r in available) / len(available)

        final_result = AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = summary,
            top_sectors = top_sectors,
            confidence  = round(avg_conf, 2),
            mode        = f"multi_agent({len(available)})",
            raw_data    = {
                "agent_results":      [r.to_dict() for r in results],
                "aggregated_sectors": aggregated,
                "final_report":       final_report,
                "agent_weights":      {r.agent_name: self.weights.get(r.agent_name, 1.0) for r in results},
            },
        )

        # 6. 通知中心推送（不影响主流程，失败仅记录警告）
        try:
            from skills.notification import get_notification_center
            get_notification_center().notify_daily_report(final_result)
        except Exception as e:
            logger.warning("[DecisionAgent] 通知推送失败: %s", e)

        return final_result

    # ------------------------------------------------------------------
    # 板块聚合逻辑
    # ------------------------------------------------------------------

    def _aggregate_sectors(self, results: list[AgentResult]) -> list[dict]:
        """
        加权汇总各 Agent 的 top_sectors。

        计算方式：
          - 每个 Agent 的板块得分 × Agent 权重 × Agent 置信度
          - 方向：多票 bullish → bullish，否则 bearish
          - 最终按加权综合分降序排列
        """
        sector_scores:   dict[str, float]       = defaultdict(float)
        sector_weights:  dict[str, float]        = defaultdict(float)
        sector_direction:dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        sector_reasons:  dict[str, list[str]]    = defaultdict(list)
        sector_agents:   dict[str, list[str]]    = defaultdict(list)

        for result in results:
            w = self.weights.get(result.agent_name, 1.0) * result.confidence
            for item in result.top_sectors:
                sector = item.get("sector", "").strip()
                if not sector:
                    continue
                raw_score = float(item.get("score", 50))
                direction = item.get("direction", "neutral")

                sector_scores[sector]  += raw_score * w
                sector_weights[sector] += w
                sector_direction[sector][direction] += w
                reason = item.get("reason", "")
                if reason:
                    sector_reasons[sector].append(f"[{result.agent_name}] {reason}")
                if result.agent_name not in sector_agents[sector]:
                    sector_agents[sector].append(result.agent_name)

        # 归一化 + 排序
        aggregated: list[dict] = []
        for sector, total_w in sector_weights.items():
            weighted_score = sector_scores[sector] / total_w if total_w else 50

            # 主方向 = 得分最高的方向
            dir_scores = sector_direction[sector]
            direction  = max(dir_scores, key=dir_scores.get) if dir_scores else "neutral"

            # 看空板块降低最终分数
            if direction == "bearish":
                weighted_score = min(weighted_score, 40)
            elif direction == "bullish":
                weighted_score = max(weighted_score, 55)

            reason_parts = sector_reasons.get(sector, [])[:3]
            aggregated.append({
                "sector":             sector,
                "weighted_score":     round(weighted_score, 1),
                "direction":          direction,
                "contributing_agents":sector_agents[sector],
                "agent_count":        len(sector_agents[sector]),
                "reason":             "；".join(reason_parts),
            })

        aggregated.sort(key=lambda x: (x["direction"] == "bullish", x["weighted_score"]), reverse=True)
        return aggregated

    # ------------------------------------------------------------------
    # LLM 综合分析
    # ------------------------------------------------------------------

    def _llm_synthesis(
        self,
        trade_date: date,
        results: list[AgentResult],
        aggregated: list[dict],
    ) -> str:
        """LLM 综合所有 Agent 报告，生成最终投资建议"""
        # 构造 Agent 摘要
        agent_summaries = []
        for r in results:
            top5 = r.top_sectors[:5]
            sectors_brief = "、".join(
                f"{s['sector']}({s.get('direction','?')})"
                for s in top5
            )
            agent_summaries.append(
                f"【{r.agent_name} Agent（置信度{r.confidence:.0%}）】\n"
                f"推荐板块：{sectors_brief}\n"
                f"{r.summary[:300]}"
            )

        # 构造聚合结果简报
        top10_text = "\n".join(
            f"{i+1}. {s['sector']}（得分:{s['weighted_score']:.0f} | "
            f"{s['direction']} | 来源:{','.join(s['contributing_agents'])}）"
            for i, s in enumerate(aggregated[:10])
        )

        prompt = f"""
你是行业板块投资决策分析师，请综合以下多个 Agent 的分析结果，给出今日板块投资决策报告。

=== 今日交易日：{trade_date} ===

=== 各 Agent 分析摘要 ===
{chr(10).join(agent_summaries)}

=== 综合评分 TOP10 板块 ===
{top10_text}

请输出一份专业的板块投资决策报告，包含：
1. **今日市场主题** — 当前市场最核心的驱动力（2-3句）
2. **重点关注板块**（3-5个）— 每个板块附：逻辑链条 + 关注理由 + 风险提示
3. **规避板块**（1-3个）— 附原因
4. **操作建议** — 仓位节奏（轻/中/满仓？分批还是一次进？）
5. **关键风险提示** — 可能导致判断失效的信号

注意：
- 聚焦板块日线级别低频操作，不涉及个股
- 综合多维度信息，不偏信单一来源
- 若各 Agent 出现明显分歧，要指出并给出倾向性判断
"""
        return self._llm.sector_prompt(prompt)

    # ------------------------------------------------------------------
    # 报告持久化
    # ------------------------------------------------------------------

    def _save_report(
        self,
        trade_date: date,
        summary: str,
        top_sectors: list[dict],
        sub_results: list[AgentResult],
    ) -> None:
        """保存完整决策报告（Markdown + JSON）"""
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)

        # Markdown 日报
        md_path = _REPORT_DIR / f"{trade_date}-decision.md"
        md_path.write_text(summary, encoding="utf-8")
        logger.info("[DecisionAgent] 已保存决策报告: %s", md_path.name)

        # JSON 结构化数据
        json_path = _REPORT_DIR / f"{trade_date}-decision.json"
        payload   = {
            "trade_date":   str(trade_date),
            "generated_at": datetime.now().isoformat(),
            "top_sectors":  top_sectors,
            "agent_results": [
                {
                    "agent_name":  r.agent_name,
                    "confidence":  r.confidence,
                    "top_sectors": r.top_sectors[:5],
                    "mode":        r.mode,
                }
                for r in sub_results
            ],
        }
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[DecisionAgent] 已保存结构化数据: %s", json_path.name)
