"""
宏观分析 Agent（板块专用版）—— 事件驱动版

设计原则：
  - 不每天强制重新分析，而是由 NBS 数据更新事件触发。
  - analyze() 先检查 macro_nbs_data 的最新 created_at（data_version）。
    若数据版本与上次分析相同，直接返回缓存结果（复用 analysis_results）。
  - force=True 可强制重新分析（由 scheduler 在每次 NBS 拉取后调用）。
  - 分析完成后自动保存到 analysis_results 表（expire_hours=720，约30天）。

Agent 链：
  1. macro_analyst    — 宏观总量（增长/通胀/就业/信用）
  2. policy_analyst   — 政策与流动性
  3. sector_mapper    — 行业配置映射（核心输出：bullish/bearish 板块）
  4. chief_strategist — 综合结论
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any

from agents.base import AgentResult, BaseAgent
from skills.llm_client import LLMClient
from skills.nbs_crawler import NBSCrawler

logger = logging.getLogger(__name__)

# 宏观分析结果有效期（小时）。月度数据，默认30天。
_MACRO_EXPIRE_HOURS = 720


def _get_nbs_data_version(engine) -> str:
    """
    获取 macro_nbs_data 表中最新记录的 created_at，用作数据版本标识。
    返回 ISO 格式字符串；若表为空则返回空字符串。
    """
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT MAX(created_at) AS latest FROM macro_nbs_data")
            ).fetchone()
            if row and row[0]:
                return str(row[0])
    except Exception as e:
        logger.debug("[MacroAgent] 获取 NBS 数据版本失败: %s", e)
    return ""


class MacroAgent(BaseAgent):
    """
    宏观分析 Agent（事件驱动）

    参数：
        top_n:       推荐板块数量上限
        skip_nbs:    跳过统计局爬虫（用于测试）
        expire_hours: 分析结果有效期（默认720小时/30天，月度数据）
    """

    name        = "macro"
    description = "宏观分析（事件驱动，NBS数据更新后触发，4专家Agent串行）"

    def __init__(
        self,
        top_n:        int  = 8,
        skip_nbs:     bool = False,
        expire_hours: int  = _MACRO_EXPIRE_HOURS,
    ):
        self.top_n        = top_n
        self.skip_nbs     = skip_nbs
        self.expire_hours = expire_hours
        self._llm         = LLMClient()
        self._crawler     = NBSCrawler()

    def is_available(self) -> bool:
        """MacroAgent 本身始终可用（数据不足时 LLM 会给出有限分析）"""
        return True

    def analyze(self, trade_date: date, force: bool = False) -> AgentResult:
        """
        分析宏观环境。

        事件驱动逻辑：
          1. 检查 macro_nbs_data 最新数据版本（created_at）
          2. 若版本与上次分析相同 且 force=False → 直接返回缓存结果
          3. 否则重新进行4-Agent串行分析并保存结果
        """
        try:
            return self._run(trade_date, force=force)
        except Exception as e:
            logger.error("[MacroAgent] 分析失败: %s", e, exc_info=True)
            return self._empty_result(trade_date, str(e))

    # ------------------------------------------------------------------
    # 内部主流程
    # ------------------------------------------------------------------

    def _run(self, trade_date: date, force: bool = False) -> AgentResult:
        from sector_heat.db import get_engine
        from skills.analysis_repo import init_analysis_table

        engine = get_engine()
        init_analysis_table(engine)

        # 1. 数据版本检查（事件驱动核心）
        nbs_version = _get_nbs_data_version(engine)
        if not force:
            cached = self.load_latest_result(engine, "macro")
            if cached and cached.raw_data.get("nbs_version") == nbs_version and nbs_version:
                logger.info(
                    "[MacroAgent] NBS 数据未更新（version=%s），复用上次分析", nbs_version[:19]
                )
                return cached

        # 2. 获取宏观数据（从已入库的 NBS 数据构建 context）
        if self.skip_nbs:
            macro_data = {"macro_indicators": {}, "market_indices": {}, "macro_news": []}
            logger.warning("[MacroAgent] skip_nbs=True，跳过统计局爬虫")
        else:
            logger.info("[MacroAgent] 开始采集宏观数据…")
            macro_data = self._crawler.fetch_all()

        context_text = self._crawler.build_prompt_context(macro_data)

        # 2. 获取本系统板块列表（用于 sector_mapper 的候选池）
        sector_pool = self._get_sector_pool()

        # 3. Agent 链
        logger.info("[MacroAgent] 运行宏观总量分析师…")
        macro_report  = self._macro_analyst(context_text)
        time.sleep(0.5)

        logger.info("[MacroAgent] 运行政策流动性分析师…")
        policy_report = self._policy_analyst(context_text)
        time.sleep(0.5)

        logger.info("[MacroAgent] 运行行业映射分析师…")
        sector_view   = self._sector_mapper(context_text, sector_pool)
        time.sleep(0.5)

        logger.info("[MacroAgent] 运行首席策略官…")
        chief_report  = self._chief_strategist(
            context_text, macro_report, policy_report, sector_view
        )

        # 4. 构造 top_sectors
        top_sectors = self._build_top_sectors(sector_view)

        # 5. 摘要
        summary = (
            f"## 宏观分析报告（{trade_date}）\n\n"
            f"### 宏观总量\n{macro_report[:500]}…\n\n"
            f"### 政策流动性\n{policy_report[:400]}…\n\n"
            f"### 综合策略\n{chief_report[:600]}…"
        )

        # 数据覆盖度（5 个指标）
        nbs_ok = len([v for v in macro_data.get("macro_indicators", {}).values() if v])
        confidence = min(1.0, nbs_ok / 5 * 0.7 + 0.3)

        result = AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = summary,
            top_sectors = top_sectors,
            confidence  = round(confidence, 2),
            mode        = "nbs_4agents",
            raw_data    = {
                "macro_report":  macro_report,
                "policy_report": policy_report,
                "sector_view":   sector_view,
                "chief_report":  chief_report,
                "context_text":  context_text[:800],
                "nbs_version":   nbs_version,  # 记录分析时的数据版本，供下次比对
            },
        )

        # 6. 保存到 Analysis Repository（30天有效期，月度数据）
        self.save_result(result, engine,
                         data_version=nbs_version,
                         expire_hours=self.expire_hours)
        logger.info("[MacroAgent] 分析结果已保存到 analysis_results（expire=%dh）", self.expire_hours)
        return result

    # ------------------------------------------------------------------
    # 各 Agent 方法
    # ------------------------------------------------------------------

    def _macro_analyst(self, context_text: str) -> str:
        prompt = f"""
你是一位资深中国宏观经济研究员。请严格基于下面的数据，分析当前国内宏观经济形势。

{context_text}

请重点回答：
1. 当前中国经济处于什么阶段，增长、通胀、就业、地产、信用各自是什么状态。
2. 当前宏观环境的核心矛盾是什么。
3. 未来1-2个季度最关键的跟踪变量有哪些。
4. 输出必须紧扣中国A股板块投资，不要空泛。
"""
        return self._llm.chat(
            [
                {"role": "system", "content": "你是中国宏观经济分析师，擅长从官方数据中提炼当前经济主线，专注板块日线级别低频交易研究。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=1800,
        )

    def _policy_analyst(self, context_text: str) -> str:
        prompt = f"""
你是一位资深的政策与流动性分析师。请基于下面的数据和新闻，评估当前中国政策环境与流动性状态。

{context_text}

请重点回答：
1. 当前政策组合更偏稳增长、稳地产、稳信用还是防风险。
2. 流动性是否对A股估值形成支撑。
3. 哪些行业板块方向更可能获得政策支持，哪些弹性偏弱。
4. 必须写出对A股板块风格和轮动的含义（不涉及个股）。
"""
        return self._llm.chat(
            [
                {"role": "system", "content": "你是中国政策与流动性分析师，擅长把政策信号映射到A股行业板块风格。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=1600,
        )

    def _sector_mapper(self, context_text: str, sector_pool: list[str]) -> dict[str, Any]:
        """返回结构化的板块配置建议（JSON）"""
        pool_str = "、".join(sector_pool[:40]) if sector_pool else "（无可用板块列表）"
        prompt = f"""
你是一位A股行业板块配置分析师。请基于宏观数据，从可选板块池中选出未来1-2个季度的受益和承压板块。

可选板块池（THS一级行业）：
{pool_str}
（以上为真实可交易的同花顺一级行业板块名称，请只从中选择）

宏观与市场上下文：
{context_text[:1500]}

请只返回 JSON，不要其他文字，格式如下：
{{
  "market_view": "震荡偏多/结构性机会/震荡偏谨慎",
  "bullish_sectors": [
    {{"sector": "银行", "logic": "具体宏观传导逻辑，不少于20字", "confidence": 0.78}},
    {{"sector": "公用事业", "logic": "逻辑", "confidence": 0.72}}
  ],
  "bearish_sectors": [
    {{"sector": "房地产", "logic": "逻辑", "confidence": 0.81}}
  ],
  "watch_signals": ["监控点1", "监控点2"]
}}

要求：
1. bullish_sectors 输出 4-6 个；
2. bearish_sectors 输出 2-4 个；
3. 板块名称必须从上方板块池中选择；
4. confidence 用 0-1 之间小数；
5. 逻辑必须结合宏观数据，不能只写"政策支持"。
"""
        result = self._llm.chat_json(
            [
                {"role": "system", "content": "你是A股行业板块配置分析师，只输出合法JSON。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=2000,
        )
        if not result:
            result = {"market_view": "数据不足", "bullish_sectors": [], "bearish_sectors": []}
        return result

    def _chief_strategist(
        self,
        context_text: str,
        macro_report: str,
        policy_report: str,
        sector_view: dict,
    ) -> str:
        prompt = f"""
你是一位首席板块策略官，需要给出当前A股板块的综合配置结论。

宏观上下文摘要：
{context_text[:600]}

【宏观总量分析师】
{macro_report[:500]}

【政策流动性分析师】
{policy_report[:500]}

【行业映射结论】
{json.dumps(sector_view, ensure_ascii=False)}

请输出一份结构清晰的综合报告，包含：
1. 当前宏观环境判断（2-3句）
2. A股板块后市展望
3. 重点看多板块及宏观传导链
4. 需要规避的板块及原因
5. 风险提示与需跟踪的数据点

注意：聚焦板块日线级别分析，不涉及个股。
"""
        return self._llm.chat(
            [
                {"role": "system", "content": "你是A股板块首席策略官，擅长把宏观结论整合为板块配置框架。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=2000,
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _get_sector_pool(self) -> list[str]:
        """从数据库获取已知板块列表（THS 一级行业）"""
        try:
            from sector_heat.db import query_heat_matrix
            rows = query_heat_matrix(lookback_days=1)
            return list({r["sector_name"] for r in rows})
        except Exception:
            return []

    def _build_top_sectors(self, sector_view: dict) -> list[dict]:
        """将 sector_mapper 的 JSON 结果转换为统一的 top_sectors 格式"""
        results: list[dict] = []
        for item in sector_view.get("bullish_sectors", [])[:self.top_n]:
            results.append({
                "sector":    item.get("sector", ""),
                "score":     min(100, int(float(item.get("confidence", 0.5)) * 100 * 1.2)),
                "direction": "bullish",
                "reason":    item.get("logic", ""),
            })
        for item in sector_view.get("bearish_sectors", []):
            results.append({
                "sector":    item.get("sector", ""),
                "score":     max(0, int((1 - float(item.get("confidence", 0.5))) * 60)),
                "direction": "bearish",
                "reason":    item.get("logic", ""),
            })
        return results
