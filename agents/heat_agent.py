"""
板块热度 Agent

使用 HeatSkill 封装层，完全解耦 sector_heat/ 内部实现。

工作流：
  1. 检查数据库中是否已有当日热度数据
  2. 若无：自动触发 HeatSkill.run_full() 完成采集+计算+写库+绘图
  3. 同步更新市场指数（上证/深证/创业板/科创50/恒生/恒生科技）
  4. 从数据库读取热度结果，转换为标准 AgentResult
  5. 将全量板块得分写入 analysis_results（json_result 为 all_sectors 列表）
"""
from __future__ import annotations

import logging
from datetime import date

from agents.base import AgentResult, BaseAgent
from skills.heat_skill import HeatSkill

logger = logging.getLogger(__name__)


class HeatAgent(BaseAgent):
    """
    板块热度 Agent

    参数：
        top_n:        输出 top_n 个最热板块
        lookback:     热力图回看天数（也是 get_heat_scores 的回看范围）
        auto_collect: True = 当日无数据时自动采集；False = 仅读库
    """

    name        = "heat"
    description = "板块热度雷达（THS 历史行情 + 指数衰减记忆模型）"

    def __init__(
        self,
        top_n: int = 15,
        lookback: int | None = None,   # None = 读 sector_heat.yaml visualizer.lookback_days
        auto_collect: bool = True,
    ):
        self.auto_collect  = auto_collect
        self.top_n         = top_n
        self._skill        = HeatSkill(lookback_days=lookback)   # HeatSkill 负责读配置
        self.lookback      = self._skill.lookback_days

    def is_available(self) -> bool:
        try:
            return self._skill.get_latest_date() is not None
        except Exception:
            return False

    def analyze(self, trade_date: date) -> AgentResult:
        try:
            return self._run(trade_date)
        except Exception as e:
            logger.error("[HeatAgent] 分析失败: %s", e, exc_info=True)
            return self._empty_result(trade_date, str(e))

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _run(self, trade_date: date) -> AgentResult:
        from sector_heat.db import get_engine
        from skills.analysis_repo import init_analysis_table
        engine = get_engine()
        init_analysis_table(engine)   # 幂等，确保表已创建

        # 1. 检查当日板块热度数据是否已存在
        today_heat = self._skill.get_heat_scores_by_date(trade_date)

        # 2. 若无数据且允许自动采集，触发完整流程（采集+计算+写库+绘图）
        if not today_heat:
            if self.auto_collect:
                logger.info("[HeatAgent] 数据库无 %s 数据，自动运行采集+计算…", trade_date)
                ok = self._skill.run_full(trade_date)
                if not ok:
                    return self._empty_result(trade_date, "run_full 失败，请检查网络和数据源")
                today_heat = self._skill.get_heat_scores_by_date(trade_date)
            else:
                return self._empty_result(
                    trade_date,
                    f"数据库无 {trade_date} 热度数据（auto_collect=False）。"
                    "请先运行: python main.py heat run",
                )

        if not today_heat:
            return self._empty_result(trade_date, "采集后数据库仍为空，采集可能失败")

        # 3. 同步更新市场指数（上证/深证/创业板/科创50/恒生/恒生科技）
        self._collect_indices(engine, trade_date)

        # 4. 原始数据（用于判断资金模式覆盖度）
        raw_rows = self._skill.get_raw_by_date(trade_date)

        # 5. 按 sort_score 排序（全量）
        def _sort_key(r):
            ss = r.get("sort_score")
            hs = r.get("heat_score")
            return float(ss) if ss is not None else (float(hs) if hs is not None else 0.0)

        sorted_heat = sorted(today_heat, key=_sort_key, reverse=True)
        top    = sorted_heat[: self.top_n]
        bottom = sorted_heat[-5:]

        # 6. 构造 top_sectors（给 DecisionAgent 使用）
        top_sectors: list[dict] = []
        for r in top:
            score = float(r.get("heat_score") or 50)
            top_sectors.append({
                "sector":    r["sector_name"],
                "score":     round(score, 1),
                "direction": "bullish" if score >= 60 else ("bearish" if score <= 35 else "neutral"),
                "reason":    _build_heat_reason(r),
            })
        for r in bottom:
            score = float(r.get("heat_score") or 50)
            if score <= 35:
                top_sectors.append({
                    "sector":    r["sector_name"],
                    "score":     round(score, 1),
                    "direction": "bearish",
                    "reason":    f"热度指数 {score:.1f}，处于低位",
                })

        # 7. 构造 all_sectors 全量得分（保存到 analysis_results，供后续统计分析）
        all_sectors = [
            {
                "sector":                   r["sector_name"],
                "score":                    round(float(r.get("heat_score")  or 0), 2),
                "sort_score":               round(float(r.get("sort_score")  or 0), 2),
                "continuous_capital_score": round(float(r.get("continuous_capital_score") or 0), 2),
                "continuous_leader_score":  round(float(r.get("continuous_leader_score")  or 0), 2),
                "rank":                     i + 1,
            }
            for i, r in enumerate(sorted_heat)
        ]

        # 8. 摘要
        hot_names  = "、".join(s["sector"] for s in top_sectors[:5] if s["direction"] == "bullish")
        cold_names = "、".join(s["sector"] for s in top_sectors if s["direction"] == "bearish")[:3]
        summary = (
            f"## 板块热度雷达（{trade_date}）\n\n"
            f"**热门板块（前5）**：{hot_names or '无'}\n\n"
            f"**冷门板块**：{cold_names or '无'}\n\n"
            "热度指数基于涨跌幅、主力净流入（当日）、连续成交量放量和连续上涨天数（历史），"
            "使用指数衰减记忆模型（decay=0.85）积累长期动量。"
        )

        # 9. 置信度
        total      = len(today_heat)
        has_inflow = sum(1 for r in raw_rows if r.get("main_net_inflow_pct") is not None)
        confidence = min(1.0, 0.5 + 0.5 * (has_inflow / max(total, 1)))

        result = AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = summary,
            top_sectors = top_sectors,
            confidence  = round(confidence, 2),
            mode        = "heat_radar",
            raw_data    = {
                "total_sectors": total,
                "lookback_days": self.lookback,
                "all_sectors":   all_sectors,          # 全量板块得分
            },
        )

        # 10. 保存到 Analysis Repository
        try:
            self.save_result(result, engine, expire_hours=20)
            logger.info("[HeatAgent] 分析结果已保存到 analysis_results（%d 个板块）", total)
        except Exception as e:
            logger.warning("[HeatAgent] 保存到 analysis_results 失败: %s", e)

        return result

    # ------------------------------------------------------------------
    # 市场指数同步
    # ------------------------------------------------------------------

    def _collect_indices(self, engine, trade_date: date) -> None:
        """
        在热度分析后同步更新市场指数数据。
        增量更新：只补齐缺失日期，已有数据不重复请求。
        失败时仅记录警告，不中断主流程。
        """
        try:
            from skills.index_skill import IndexSkill

            n_new = IndexSkill().fetch_and_store(
                engine,
                end_date    = trade_date,
                incremental = True,          # 仅补齐缺失日期
            )
            if n_new:
                logger.info("[HeatAgent] 市场指数更新：+%d 条记录", n_new)
            else:
                logger.debug("[HeatAgent] 市场指数已是最新，无需更新")
        except Exception as e:
            logger.warning("[HeatAgent] 市场指数更新失败（不影响热度分析）: %s", e)


def _build_heat_reason(r: dict) -> str:
    parts = []
    hs = r.get("heat_score")
    if hs:
        parts.append(f"热度指数 {float(hs):.1f}")
    cc = r.get("continuous_capital_score")
    if cc and float(cc) > 100:
        parts.append(f"连续资金分 {float(cc):.0f}")
    cl = r.get("continuous_leader_score")
    if cl and float(cl) > 100:
        parts.append(f"连续领涨分 {float(cl):.0f}")
    return "，".join(parts) if parts else ""
