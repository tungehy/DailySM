"""
研报观点 Agent（时效加权 RAG 版）

定位：
  基于手动导入的券商研报知识库（research_reports / research_chunks），
  用「时效加权 RAG」检索出仍然新鲜且相关的研报片段，LLM 抽取行业观点，
  统一写入 market_opinions（source_type=research），供 DecisionAgent 聚合。

双模式（both）：
  1. 自动抽观点：analyze() 针对每个关注行业检索 → LLM 抽观点 → market_opinions
  2. 知识库问答：retrieve(query) 暴露检索接口，供决策/问答时按需调用

三层架构中的位置：
  Raw+Vector 层：research_reports / research_chunks（手动导入 + 向量切块）
        │  时效加权检索 + LLM 抽取
        ▼
  Opinion 层：market_opinions（source_type=research，horizon=long）
        ▼
  Analysis 层：analysis_results（agent_name=research）

数据录入：
  手动导入，见 `python main.py research import <文件>`（支持 docx/pdf/md/json/txt）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from agents.base import AgentResult, BaseAgent
from skills.llm_client import LLMClient

logger = logging.getLogger(__name__)


class ResearchAgent(BaseAgent):
    """
    研报观点 Agent（时效加权 RAG）

    参数：
        top_n:            输出板块数量
        top_k:            每个行业检索的研报片段数
        focus_industries: 关注行业列表；None/空 = 自动取库中仍新鲜的行业
    """

    name        = "research"
    description = "券商研报观点（时效加权 RAG → 统一市场观点库 market_opinions）"

    def __init__(
        self,
        top_n:            int = 8,
        top_k:            int | None = None,
        focus_industries: list[str] | None = None,
    ):
        self._llm    = LLMClient()
        self._embed  = None   # 懒加载
        cfg          = self._load_cfg()
        self.top_n            = top_n
        self.top_k            = top_k if top_k is not None else int(cfg.get("top_k", 6))
        self.drop_threshold   = float(cfg.get("drop_threshold", 0.15))
        self.focus_industries = focus_industries if focus_industries is not None else \
            (cfg.get("focus_industries") or [])
        self.expire_hours     = int(cfg.get("expire_hours", 24 * 7))

    # ------------------------------------------------------------------
    # 懒加载依赖
    # ------------------------------------------------------------------

    @property
    def embed(self):
        if self._embed is None:
            from skills.embedding_client import EmbeddingClient
            self._embed = EmbeddingClient()
        return self._embed

    @staticmethod
    def _load_cfg() -> dict:
        try:
            from skills.research_db import load_research_cfg
            return load_research_cfg()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """库中存在仍新鲜的研报即视为可用"""
        try:
            from sector_heat.db import get_engine
            from skills.research_db import get_known_industries
            engine = get_engine()
            return len(get_known_industries(engine, min_decay=self.drop_threshold)) > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 公开检索接口（知识库问答模式）
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query:    str,
        top_k:    int | None = None,
        industry: str | None = None,
        as_of:    datetime | None = None,
    ) -> list[dict]:
        """
        时效加权检索研报片段，供外部（决策/问答）直接调用。
        返回带 similarity / decay / score 的片段列表。
        """
        from sector_heat.db import get_engine
        from skills.research_db import search
        engine = get_engine()
        return search(
            engine, self.embed, query,
            top_k          = top_k or self.top_k,
            industry       = industry,
            as_of          = as_of,
            drop_threshold = self.drop_threshold,
        )

    # ------------------------------------------------------------------
    # 分析（自动抽观点模式）
    # ------------------------------------------------------------------

    def analyze(self, trade_date: date) -> AgentResult:
        try:
            return self._run(trade_date)
        except Exception as e:
            logger.error("[ResearchAgent] 分析失败: %s", e, exc_info=True)
            return self._empty_result(trade_date, str(e))

    def _run(self, trade_date: date) -> AgentResult:
        from sector_heat.db import get_engine
        from skills.research_db import get_known_industries
        from skills.market_opinion_db import (
            init_market_opinion_table, insert_opinions,
            delete_opinions_for_source_date,
        )
        from skills.analysis_repo import init_analysis_table

        engine = get_engine()
        init_market_opinion_table(engine)
        init_analysis_table(engine)

        td    = str(trade_date)
        as_of = datetime(trade_date.year, trade_date.month, trade_date.day, 23, 59)

        # 确定关注行业
        industries = self.focus_industries or get_known_industries(
            engine, as_of=as_of, min_decay=self.drop_threshold
        )
        if not industries:
            return self._empty_result(
                trade_date, "研报知识库为空或无新鲜研报（请先 import 研报）"
            )

        logger.info("[ResearchAgent] 关注 %d 个行业：%s",
                    len(industries), "、".join(industries[:10]))

        # 逐行业检索 + LLM 抽观点
        all_opinions: list[dict] = []
        for industry in industries:
            chunks = self.retrieve(
                query    = f"{industry}行业最新景气度、投资逻辑与配置建议",
                industry = industry,
                as_of    = as_of,
            )
            if not chunks:
                continue
            opinion = self._extract_opinion(industry, chunks, td)
            if opinion:
                all_opinions.append(opinion)

        if not all_opinions:
            return self._empty_result(trade_date, "未能从研报抽取到有效行业观点")

        # 写统一观点库（幂等重跑先清后写）
        delete_opinions_for_source_date(engine, "research", td)
        insert_opinions(engine, all_opinions)
        logger.info("[ResearchAgent] 写入市场观点库：%d 条", len(all_opinions))

        # 汇总 + 存 analysis_results
        top_sectors = self._build_top_sectors(all_opinions)
        summary     = self._build_summary(trade_date, all_opinions)
        result = AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = summary,
            top_sectors = top_sectors,
            confidence  = 0.75,
            mode        = "research_rag_timedecay",
            raw_data    = {
                "industries_covered": len(all_opinions),
                "opinions":           all_opinions,
            },
        )
        try:
            self.save_result(result, engine, expire_hours=self.expire_hours)
        except Exception as e:
            logger.warning("[ResearchAgent] 保存 analysis_results 失败: %s", e)

        return result

    # ------------------------------------------------------------------
    # LLM 抽取单行业观点
    # ------------------------------------------------------------------

    def _extract_opinion(self, industry: str, chunks: list[dict], td: str) -> dict | None:
        """从检索到的研报片段中抽取该行业的方向性观点"""
        # 拼接片段（附来源与新鲜度提示）
        ctx_lines = []
        for i, c in enumerate(chunks):
            src = c.get("institution") or c.get("report_title") or "研报"
            ctx_lines.append(
                f"[片段{i+1} | 来源:{src} | 新鲜度:{c.get('decay', 0):.2f}]\n{c['content'][:600]}"
            )
        context = "\n\n".join(ctx_lines)[:6000]

        prompt = (
            f"以下是关于「{industry}」行业的券商研报片段（已按时效加权检索）。\n"
            f"请综合这些观点，判断该行业当前的投资方向。\n\n"
            f"=== 研报片段 ===\n{context}\n\n"
            f"只输出纯 JSON：\n"
            f'{{"direction":"bullish/bearish/neutral",'
            f'"strength":<0~100 观点强度整数>,'
            f'"confidence":<0~1 置信度>,'
            f'"reason":"60字内核心逻辑"}}\n'
            f"注意：若片段观点矛盾或不足以判断，direction 用 neutral。"
        )
        result = self._llm.chat_json(
            [
                {"role": "system", "content": "你是券商行业研究员，只输出纯JSON。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=500,
        )
        if not result or not result.get("direction"):
            return None

        # 用检索片段的最高时效分作为该观点的 confidence 修正
        best_decay = max((c.get("decay", 0) for c in chunks), default=0.5)
        institutions = sorted({c.get("institution") for c in chunks if c.get("institution")})

        return {
            "source_type":  "research",
            "source_name":  "、".join(institutions[:3]) or "券商研报",
            "raw_ref":      f"research:{industry}:{td}",
            "target_type":  "sector",
            "target_name":  industry,
            "direction":    result.get("direction", "neutral"),
            "strength":     float(result.get("strength") or 60),
            "confidence":   min(1.0, float(result.get("confidence") or 0.6) * (0.6 + 0.4 * best_decay)),
            "reason":       str(result.get("reason", ""))[:300],
            "horizon":      "long",        # 研报观点时效长（90 天）
            "publish_time": td,
            "trade_date":   td,
            "extra":        {"chunk_count": len(chunks), "institutions": institutions},
        }

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

    def _build_top_sectors(self, opinions: list[dict]) -> list[dict]:
        ranked = sorted(
            opinions,
            key=lambda o: (o["direction"] == "bullish", o.get("strength", 0)),
            reverse=True,
        )
        return [
            {
                "sector":    o["target_name"],
                "score":     int(o.get("strength") or 60),
                "direction": o.get("direction", "neutral"),
                "reason":    o.get("reason", ""),
            }
            for o in ranked[: self.top_n]
        ]

    def _build_summary(self, trade_date: date, opinions: list[dict]) -> str:
        bull = [o["target_name"] for o in opinions if o["direction"] == "bullish"]
        bear = [o["target_name"] for o in opinions if o["direction"] == "bearish"]
        return (
            f"## 研报观点分析（{trade_date}）\n\n"
            f"覆盖 {len(opinions)} 个行业（时效加权 RAG 检索）。\n\n"
            f"**研报看多**：{('、'.join(bull[:6])) or '无'}\n\n"
            f"**研报看空**：{('、'.join(bear[:4])) or '无'}"
        )
