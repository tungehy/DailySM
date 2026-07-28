"""
对话决策 Agent（ChatAgent）—— 交互式个性化投资顾问

三步式混合架构（每轮对话）：
  ① 意图理解：LLM 解析用户提问 → 关注行业、意图类型、是否需补新闻、持仓披露
  ② 按需补数据：
       - 实时补采：定向获取相关行业最新新闻（智能：优先读当日 news_raw，不足才重爬）
       - 存量融合：market_opinions（统一观点库）+ 各 Agent 最新分析（heat/quant/macro/research）
       - 研报 RAG：ResearchAgent.retrieve() 时效加权检索
       - 用户持仓：user_portfolio
  ③ 融合生成：LLM 结合全部上下文 + 持仓，给出个性化建议

设计原则：
  - 不重复造数据：优先复用各 Agent 已产出的分析结果和统一观点库
  - 持仓自动记忆：对话中披露的持仓自动解析入库（user_portfolio）
  - 会话记忆：内存维护多轮上下文，并持久化到 chat_history
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from skills.llm_client import LLMClient

logger = logging.getLogger(__name__)


class ChatAgent:
    """交互式对话决策 Agent"""

    def __init__(self, session_id: str | None = None, news_min: int = 3):
        self._llm      = LLMClient()
        self._embed    = None
        self._research = None
        self.session_id = session_id or datetime.now().strftime("%Y%m%d%H%M%S")
        self.news_min   = news_min          # 当日相关新闻少于此值时触发实时补采
        self.history: list[dict] = []       # 会话内记忆 [{role, content}]

    # ------------------------------------------------------------------
    # 懒加载依赖
    # ------------------------------------------------------------------

    @property
    def research(self):
        if self._research is None:
            from agents.research_agent import ResearchAgent
            self._research = ResearchAgent()
        return self._research

    @staticmethod
    def _engine():
        from sector_heat.db import get_engine
        return get_engine()

    # ------------------------------------------------------------------
    # 对话主入口
    # ------------------------------------------------------------------

    def chat(self, user_message: str) -> str:
        """处理一轮用户提问，返回回答文本"""
        try:
            return self._run(user_message)
        except Exception as e:
            logger.error("[ChatAgent] 处理失败: %s", e, exc_info=True)
            return f"抱歉，处理你的问题时出错了：{e}"

    def _run(self, user_message: str) -> str:
        engine = self._engine()

        # ① 意图理解
        intent = self._understand_intent(user_message)
        industries = intent.get("industries", []) or []
        self._last_industries = industries   # 供 API 层读取（推理依据/前端展示）
        logger.info("[ChatAgent] 意图=%s 行业=%s 需补新闻=%s",
                    intent.get("intent"), industries, intent.get("need_fresh_news"))

        # ①.5 持仓披露 → 自动入库
        self._save_disclosed_holdings(engine, intent.get("portfolio_updates", []))

        # ② 按需补数据
        context = self._gather_context(engine, user_message, intent)

        # ③ 融合生成
        answer = self._synthesize(user_message, context)

        # 记忆 + 持久化
        self._remember("user", user_message)
        self._remember("assistant", answer)
        self._persist(engine, user_message, answer)
        return answer

    # ------------------------------------------------------------------
    # ① 意图理解
    # ------------------------------------------------------------------

    def _understand_intent(self, user_message: str) -> dict:
        hist_hint = self._recent_history_text(turns=3)
        prompt = (
            "你是A股投资对话助手的意图理解模块。分析用户提问，输出结构化意图。\n\n"
            + (f"【最近对话】\n{hist_hint}\n\n" if hist_hint else "")
            + f"【用户提问】\n{user_message}\n\n"
            "请只输出纯JSON：\n"
            "{\n"
            '  "industries": ["涉及的申万/同花顺一级行业名，如 半导体、医药生物；无则空数组"],\n'
            '  "intent": "配置建议/持仓诊断/行情解读/概念科普/其他",\n'
            '  "need_fresh_news": true/false,  // 用户是否关心最新消息面（问"最新""最近""今天"等时为true）\n'
            '  "portfolio_updates": [  // 用户本次提到的自己的持仓；没有则空数组\n'
            '    {"name":"标的名","holding_type":"sector/stock/etf/theme","position_pct":仓位数字或null,"cost_price":成本或null,"shares":股数或null}\n'
            "  ]\n"
            "}\n"
            "注意：行业名要规范化为标准一级行业；用户说“芯片”映射为“半导体”，“白酒”映射为“食品饮料”等。"
        )
        result = self._llm.chat_json(
            [
                {"role": "system", "content": "你是意图理解模块，只输出纯JSON。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=600,
        )
        if not isinstance(result, dict):
            return {"industries": [], "intent": "其他", "need_fresh_news": False, "portfolio_updates": []}
        return result

    def _save_disclosed_holdings(self, engine, updates: list[dict]) -> None:
        if not updates:
            return
        from skills.portfolio_db import init_portfolio_table, upsert_holding
        init_portfolio_table(engine)
        for h in updates:
            if h.get("name"):
                upsert_holding(engine, h)
        logger.info("[ChatAgent] 从对话解析并更新 %d 条持仓", len(updates))

    # ------------------------------------------------------------------
    # ② 按需补数据
    # ------------------------------------------------------------------

    def _gather_context(self, engine, user_message: str, intent: dict) -> dict:
        industries      = intent.get("industries", []) or []
        need_fresh_news = bool(intent.get("need_fresh_news"))
        today           = date.today()

        ctx: dict[str, Any] = {
            "industries":  industries,
            "opinions":    {},   # industry -> aggregated opinion
            "heat":        {},   # industry -> heat row
            "research":    {},   # industry -> [chunks]
            "fresh_news":  [],   # list of news items
            "agent_briefs": {},  # agent_name -> summary
            "holdings":    [],
        }

        # 2.1 持仓
        try:
            from skills.portfolio_db import init_portfolio_table, get_holdings
            init_portfolio_table(engine)
            ctx["holdings"] = get_holdings(engine)
        except Exception as e:
            logger.debug("读取持仓失败: %s", e)

        # 2.2 每个行业：市场观点 + 热度 + 研报 RAG
        for ind in industries:
            ctx["opinions"][ind]  = self._industry_opinions(engine, ind)
            ctx["heat"][ind]      = self._industry_heat(engine, ind, today)
            ctx["research"][ind]  = self._industry_research(ind)

        # 2.3 新闻：智能补采
        ctx["fresh_news"] = self._gather_news(engine, industries, need_fresh_news, today)

        # 2.4 各 Agent 最新分析摘要
        ctx["agent_briefs"] = self._agent_briefs(engine)

        return ctx

    def _industry_opinions(self, engine, industry: str) -> dict | None:
        """统一观点库中该行业的聚合观点"""
        try:
            from skills.market_opinion_db import get_active_opinions
            ops = get_active_opinions(engine, target_name=industry, target_type="sector")
            if not ops:
                return None
            bull = sum(o.get("effective_weight", 0) for o in ops if o["direction"] == "bullish")
            bear = sum(o.get("effective_weight", 0) for o in ops if o["direction"] == "bearish")
            total = bull + bear + 1e-9
            net = (bull - bear) / total
            reasons = [o["reason"] for o in ops[:4] if o.get("reason")]
            srcs = sorted({o.get("source_type") for o in ops if o.get("source_type")})
            return {
                "net_score":  round(net, 2),
                "direction":  "bullish" if net > 0.15 else ("bearish" if net < -0.15 else "neutral"),
                "count":      len(ops),
                "sources":    srcs,
                "reasons":    reasons,
            }
        except Exception as e:
            logger.debug("查询观点失败(%s): %s", industry, e)
            return None

    def _industry_heat(self, engine, industry: str, today: date) -> dict | None:
        """该行业最新热度"""
        try:
            from skills.heat_skill import HeatSkill
            skill = HeatSkill()
            latest = skill.get_latest_date()
            if not latest:
                return None
            rows = skill.get_heat_scores_by_date(latest)
            row = next((r for r in rows if r.get("sector_name") == industry), None)
            if not row:
                return None
            return {
                "trade_date": str(latest),
                "heat_score": float(row.get("heat_score") or 0),
                "sort_score": float(row.get("sort_score") or 0),
            }
        except Exception as e:
            logger.debug("查询热度失败(%s): %s", industry, e)
            return None

    def _industry_research(self, industry: str) -> list[dict]:
        """研报 RAG 时效加权检索"""
        try:
            hits = self.research.retrieve(
                query=f"{industry}行业投资逻辑与配置建议", industry=industry, top_k=3
            )
            return [
                {
                    "content":     h["content"][:300],
                    "institution": h.get("institution"),
                    "score":       h.get("score"),
                    "decay":       h.get("decay"),
                }
                for h in hits
            ]
        except Exception as e:
            logger.debug("研报检索失败(%s): %s", industry, e)
            return []

    def _gather_news(self, engine, industries: list[str], need_fresh: bool, today: date) -> list[dict]:
        """
        智能新闻补采：
          1. 先查当日已入库 news_raw 中与行业相关的新闻
          2. 若不足 news_min 或用户要求最新 → 触发全网定向重爬（本地关键词过滤）
        """
        related = self._query_stored_news(engine, industries, today)

        # 无具体行业且不要求最新 → 直接用已入库新闻，不触发耗时的全网重爬
        if not industries and not need_fresh:
            return related[:8]

        if len(related) >= self.news_min and not need_fresh:
            logger.info("[ChatAgent] 命中当日已入库相关新闻 %d 条，不重爬", len(related))
            return related[:8]

        # 触发实时定向补采
        logger.info("[ChatAgent] 触发实时新闻补采（行业=%s）…", industries or "全部")
        try:
            from skills.news_crawler import NewsCrawler
            crawler = NewsCrawler()
            data = crawler.fetch_all(categories=["finance", "news"])
            fresh = crawler.extract_finance_news(
                data, extra_keywords=industries or None, top_n=12
            )
            # 若指定了行业，进一步按行业词过滤
            if industries:
                filtered = [n for n in fresh if any(ind in n["title"] for ind in industries)]
                fresh = filtered or fresh[:6]
            return fresh[:8]
        except Exception as e:
            logger.warning("[ChatAgent] 实时新闻补采失败: %s", e)
            return related[:8]

    def _query_stored_news(self, engine, industries: list[str], today: date) -> list[dict]:
        """查当日 news_raw 中与行业相关的新闻"""
        try:
            from skills.news_db import get_all_news_for_date
            rows = get_all_news_for_date(engine, str(today))
        except Exception:
            return []
        if not industries:
            return [{"title": r["title"], "platform": r.get("platform", ""),
                     "summary": r.get("summary")} for r in rows[:8]]
        hits = []
        for r in rows:
            hay = (r.get("title", "") + " " + (r.get("summary") or "")
                   + " " + " ".join(str(k) for k in (r.get("keywords") or [])))
            if any(ind in hay for ind in industries):
                hits.append({"title": r["title"], "platform": r.get("platform", ""),
                             "summary": r.get("summary")})
        return hits

    def _agent_briefs(self, engine) -> dict:
        """各 Agent 最新分析摘要（存量融合）"""
        briefs = {}
        specs = [
            ("heat", ""), ("quant", ""), ("macro", ""),
            ("news", ""), ("research", ""), ("decision", "decision"),
        ]
        try:
            from skills.analysis_repo import load_latest_as_result
        except Exception:
            return briefs
        for agent_name, atype in specs:
            try:
                res = load_latest_as_result(engine, agent_name, atype)
                if res and res.summary:
                    briefs[agent_name] = {
                        "trade_date":  str(res.trade_date),
                        "summary":     res.summary[:500],
                        "top_sectors": res.top_sectors[:8],
                    }
            except Exception:
                continue
        return briefs

    # ------------------------------------------------------------------
    # ③ 融合生成
    # ------------------------------------------------------------------

    def _synthesize(self, user_message: str, ctx: dict) -> str:
        from skills.portfolio_db import format_holdings

        sections = []

        # 持仓
        sections.append("【用户持仓】\n" + format_holdings(ctx["holdings"]))

        # 行业观点/热度/研报
        for ind in ctx["industries"]:
            block = [f"◆ 行业：{ind}"]
            op = ctx["opinions"].get(ind)
            if op:
                block.append(
                    f"  · 市场观点：{op['direction']}（净分{op['net_score']}，"
                    f"{op['count']}条，来源{op['sources']}）"
                )
                for r in op["reasons"][:3]:
                    block.append(f"    - {r}")
            ht = ctx["heat"].get(ind)
            if ht:
                block.append(f"  · 热度指数：{ht['heat_score']:.1f}（{ht['trade_date']}）")
            rs = ctx["research"].get(ind) or []
            for r in rs[:2]:
                block.append(f"  · 研报（{r.get('institution') or '-'}，新鲜度{r.get('decay')}）：{r['content'][:120]}")
            sections.append("\n".join(block))

        # 最新新闻
        if ctx["fresh_news"]:
            news_lines = ["【相关最新新闻】"]
            for n in ctx["fresh_news"][:8]:
                news_lines.append(f"  - [{n.get('platform','')}] {n['title']}")
            sections.append("\n".join(news_lines))

        # 各 Agent 分析
        if ctx["agent_briefs"]:
            brief_lines = ["【各分析模块最新结论】"]
            name_cn = {"heat": "板块热度", "quant": "量化", "macro": "宏观",
                       "news": "新闻舆情", "research": "研报", "decision": "综合决策"}
            for agent, b in ctx["agent_briefs"].items():
                tops = "、".join(s.get("sector", "") for s in b["top_sectors"][:5])
                brief_lines.append(f"  · {name_cn.get(agent, agent)}（{b['trade_date']}）：{tops}")
            sections.append("\n".join(brief_lines))

        context_text = "\n\n".join(sections)
        hist = self._recent_history_text(turns=4)

        prompt = (
            "你是资深A股投资顾问。请结合以下多维度信息，回答用户问题，给出有针对性的建议。\n\n"
            + (f"=== 最近对话 ===\n{hist}\n\n" if hist else "")
            + f"=== 参考信息 ===\n{context_text}\n\n"
            + f"=== 用户问题 ===\n{user_message}\n\n"
            "要求：\n"
            "1. 紧扣用户问题和其持仓情况，给出个性化、可操作的建议\n"
            "2. 有理有据，引用上面的观点/热度/研报/新闻，但不编造未提供的数据\n"
            "3. 结构清晰（结论先行，再展开逻辑，最后风险提示）\n"
            "4. 只分析行业/板块层面，涉及个股时提示需自行研判\n"
            "5. 必须包含风险提示，说明这不构成投资建议"
        )
        return self._llm.chat(
            [
                {"role": "system", "content": "你是资深A股投资顾问，客观、专业、注重风险。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=1500,
            temperature=0.6,
            include_reasoning=False,
        )

    # ------------------------------------------------------------------
    # 记忆
    # ------------------------------------------------------------------

    def _remember(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        self.history = self.history[-20:]   # 只保留最近 20 条

    def _recent_history_text(self, turns: int = 3) -> str:
        if not self.history:
            return ""
        recent = self.history[-turns * 2:]
        return "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}：{m['content'][:200]}"
            for m in recent
        )

    def _persist(self, engine, user_message: str, answer: str) -> None:
        try:
            from skills.chat_db import init_chat_table, save_message
            init_chat_table(engine)
            save_message(engine, self.session_id, "user", user_message)
            save_message(engine, self.session_id, "assistant", answer)
        except Exception as e:
            logger.debug("持久化对话失败: %s", e)
