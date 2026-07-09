"""
新闻流量 Agent（三层架构版）

数据流：
  NewsCrawler → news_raw（status=pending）
      ↓
  NewsAgent（本模块）
      ↓ 行业映射 LLM 分析
  news_industry_mapping（Feature Layer）
      ↓ 汇总生成今日舆情报告
  analysis_results（Analysis Layer）
      ↓ 将 news_raw 标为 completed

设计原则：
  - Agent 不直接读取爬虫结果，统一读取 news_raw（Raw Layer）
  - Skill 只负责采集/预处理/存储，不生成投资结论
  - 支持 Prompt 升级后对历史新闻重新分析（reset + re-run）
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date
from typing import Any

from agents.base import AgentResult, BaseAgent
from skills.llm_client import LLMClient
from skills.news_crawler import NewsCrawler

logger = logging.getLogger(__name__)


class NewsAgent(BaseAgent):
    """
    新闻流量 Agent（三层架构）

    外部接口：
        analyze(trade_date)     → AgentResult
        recrawl_and_reanalyze(trade_date)  → AgentResult（强制重爬+重分析）

    内部流程：
        1. 触发 NewsCrawler.fetch_and_store() 采集新闻（若当天无 pending）
        2. 读取 news_raw status=pending 数据
        3. 批量行业映射 LLM 分析 → news_industry_mapping
        4. 汇总生成 AgentResult → analysis_results
        5. 标记 news_raw status=completed
    """

    name        = "news"
    description = "新闻流量监测（22平台 → news_raw → 行业映射 → 舆情分析）"

    def __init__(
        self,
        categories:  list[str] | None = None,
        top_n:       int = 8,
        max_items:   int = 40,
        batch_size:  int = 10,
    ):
        self.categories = categories if categories is not None else ["finance", "news"]
        self.top_n      = top_n
        self.max_items  = max_items
        self.batch_size = batch_size
        self._llm       = LLMClient()
        self._crawler   = NewsCrawler(llm_client=self._llm)

    def is_available(self) -> bool:
        try:
            import requests  # noqa
            return True
        except ImportError:
            return False

    def analyze(self, trade_date: date, force: bool = False) -> AgentResult:
        """
        分析指定交易日的新闻舆情。

        Args:
            force: True 则重置当天 pending 状态并重新采集分析
        """
        try:
            return self._run(trade_date, force=force)
        except Exception as e:
            logger.error("[NewsAgent] 分析失败: %s", e, exc_info=True)
            return self._empty_result(trade_date, str(e))

    def recrawl_and_reanalyze(self, trade_date: date) -> AgentResult:
        """强制重爬+重分析（用于 Prompt 升级或数据修正）"""
        return self.analyze(trade_date, force=True)

    # ------------------------------------------------------------------
    # 内部主流程
    # ------------------------------------------------------------------

    def _run(self, trade_date: date, force: bool = False) -> AgentResult:
        from sector_heat.db import get_engine
        from skills.news_db import (
            init_news_tables, get_pending_news, count_news_for_date,
            delete_mappings_for_date, insert_industry_mappings,
            get_mappings_for_date, mark_news_status, reset_news_to_pending,
        )

        engine = get_engine()
        init_news_tables(engine)

        td = str(trade_date)

        # ── Step 1：确保有 pending 新闻 ──────────────────────────────────
        if force:
            # 重置当天已完成/失败的记录，触发重新分析
            reset_cnt = reset_news_to_pending(engine, td)
            if reset_cnt:
                logger.info("[NewsAgent] force=True：重置 %d 条新闻为 pending", reset_cnt)
            delete_mappings_for_date(engine, td)

        pending = get_pending_news(engine, td)

        if not pending:
            # 触发采集（当天未爬取，或已全部完成且非 force 模式）
            counts = count_news_for_date(engine, td)
            if counts.get("completed", 0) > 0 and not force:
                # 当天已分析完成，直接从 analysis_results 复用
                logger.info(
                    "[NewsAgent] 当天新闻已全部分析完成（completed=%d），复用 analysis_results",
                    counts["completed"],
                )
                from skills.analysis_repo import load_if_fresh
                cached = load_if_fresh(engine, self.name, max_age_hours=20)
                if cached:
                    return self.load_latest_result(engine)

            # 触发新采集
            logger.info("[NewsAgent] 触发 NewsCrawler 采集（categories=%s）…", self.categories)
            n_inserted = self._crawler.fetch_and_store(
                trade_date  = td,
                engine      = engine,
                categories  = self.categories,
                max_items   = self.max_items,
                batch_size  = self.batch_size,
            )
            logger.info("[NewsAgent] 新增 %d 条新闻入库", n_inserted)
            pending = get_pending_news(engine, td)

        if not pending:
            return self._empty_result(trade_date, "采集失败或无新闻数据")

        logger.info("[NewsAgent] 开始行业映射分析（%d 条 pending 新闻）…", len(pending))

        # ── Step 2：读取流量分（来自第一条新闻的 extra 字段）───────────────
        flow_score = self._extract_flow_score(pending)

        # ── Step 3：行业映射 LLM 分析 ────────────────────────────────────
        delete_mappings_for_date(engine, td)   # 清除旧映射（支持幂等重跑）
        mapping_raw = self._industry_mapping_agent(pending, trade_date)

        # 验证并写库
        mapping_records = []
        for m in mapping_raw:
            news_id = m.get("news_id")
            if not news_id:
                continue
            industry = str(m.get("industry_name", "")).strip()
            if not industry:
                continue
            mapping_records.append({
                "news_id":       news_id,
                "trade_date":    td,
                "industry_name": industry,
                "sentiment":     m.get("sentiment", "neutral"),
                "impact_score":  _clamp(m.get("impact_score"), 0.0, 1.0),
                "confidence":    _clamp(m.get("confidence"),   0.0, 1.0),
                "reason":        str(m.get("reason", ""))[:300],
            })
        insert_industry_mappings(engine, mapping_records)

        # ── Step 4：汇总行业映射 → top_sectors ──────────────────────────
        all_mappings  = get_mappings_for_date(engine, td)
        top_sectors   = self._build_top_sectors(all_mappings)
        sentiment_info = self._calc_sentiment(pending, all_mappings, flow_score)
        summary       = self._build_summary(trade_date, top_sectors, sentiment_info, flow_score)

        success_rate  = flow_score.get("active_platforms", 0) / max(
            sum(1 for p in pending if p.get("source") != "hot_topic"), 1
        )
        confidence    = round(min(1.0, 0.2 + success_rate * 0.6 + min(len(all_mappings) / 30, 0.2)), 2)

        result = AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = summary,
            top_sectors = top_sectors,
            confidence  = confidence,
            mode        = f"news_3tier_{'+'.join(self.categories)}",
            raw_data    = {
                "flow_score":     flow_score,
                "news_count":     len(pending),
                "mapping_count":  len(all_mappings),
                "top_industries": _top_industries(all_mappings, n=10),
                "sentiment_info": sentiment_info,
            },
        )

        # ── Step 5：保存 analysis_results ────────────────────────────────
        try:
            self.save_result(result, engine, expire_hours=20)
        except Exception as e:
            logger.warning("[NewsAgent] 保存到 analysis_results 失败: %s", e)

        # ── Step 6：标记 news_raw 为 completed ───────────────────────────
        mark_news_status(engine, [n["id"] for n in pending], "completed")
        logger.info("[NewsAgent] 分析完成，%d 条新闻标记 completed", len(pending))

        return result

    # ------------------------------------------------------------------
    # LLM：行业映射分析（核心）
    # ------------------------------------------------------------------

    # 每批处理的新闻条数上限（控制输出 token 量，防止截断）
    _MAPPING_BATCH_SIZE = 15

    def _industry_mapping_agent(
        self,
        pending_news: list[dict],
        trade_date:   date,
    ) -> list[dict]:
        """
        分批分析所有 pending 新闻，输出行业映射列表。

        每批最多 _MAPPING_BATCH_SIZE 条，避免输出 token 超限被截断。
        各批结果合并后返回。

        每条映射：{news_id, industry_name, sentiment, impact_score, confidence, reason}
        一条新闻可映射 1~4 个行业。
        """
        all_mappings: list[dict] = []
        news_list = pending_news[:50]   # 最多处理 50 条
        batch_size = self._MAPPING_BATCH_SIZE

        for batch_start in range(0, len(news_list), batch_size):
            batch = news_list[batch_start:batch_start + batch_size]
            batch_mappings = self._mapping_batch(batch, trade_date)
            all_mappings.extend(batch_mappings)
            logger.info(
                "[NewsAgent] 行业映射批次 %d/%d 完成：+%d 条映射",
                batch_start // batch_size + 1,
                (len(news_list) + batch_size - 1) // batch_size,
                len(batch_mappings),
            )

        logger.info(
            "[NewsAgent] 行业映射全部完成：%d 条映射（来自 %d 条新闻）",
            len(all_mappings), len(news_list),
        )
        return all_mappings

    def _mapping_batch(self, batch: list[dict], trade_date: date) -> list[dict]:
        """对单批新闻（≤ _MAPPING_BATCH_SIZE 条）调用 LLM 完成行业映射"""
        news_lines = []
        for i, news in enumerate(batch):
            summary    = news.get("summary") or news["title"]
            keywords   = news.get("keywords") or []
            event_type = news.get("event_type") or ""
            line = f"{i+1}. [ID:{news['id']}] {news['title']}"
            if summary and summary != news["title"]:
                line += f"\n   摘要: {summary[:100]}"
            if keywords:
                kw_str = ", ".join(str(k) for k in (keywords if isinstance(keywords, list) else [])[:5])
                if kw_str:
                    line += f"\n   关键词: {kw_str}"
            if event_type:
                line += f"\n   类型: {event_type}"
            news_lines.append(line)

        news_text = "\n".join(news_lines)

        prompt = (
            f"你是专注A股的行业分析师。分析以下 {len(batch)} 条新闻，"
            f"判断每条对哪些A股行业板块有影响。\n\n"
            f"=== 新闻列表（{trade_date}）===\n{news_text}\n\n"
            f"分析规则：\n"
            f"1. 每条新闻可映射 1~4 个行业（无实质影响的新闻不必输出）\n"
            f"2. 行业名称使用同花顺一级行业分类（如：电子、医药生物、机械设备、新能源汽车等）\n"
            f"3. sentiment: bullish=利好 / bearish=利空 / neutral=中性\n"
            f"4. impact_score: 0~1，影响强度（0.8以上为重大影响）\n"
            f"5. confidence: 0~1，判断置信度\n"
            f"6. news_id 必须与上方 [ID:xxx] 完全一致（整数）\n\n"
            f"只输出纯 JSON，不要任何说明文字：\n"
            f'{{"mappings": ['
            f'{{"news_id":<整数>,"industry_name":"行业名","sentiment":"bullish/bearish/neutral",'
            f'"impact_score":0.8,"confidence":0.75,"reason":"50字内原因"}}]}}'
        )

        result = self._llm.chat_json(
            [
                {"role": "system", "content": "你是A股行业分析师，只输出纯JSON，不包含任何说明文字。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=2500,
        )

        if not result:
            logger.warning("[NewsAgent] 批次行业映射 LLM 返回空结果（%d 条新闻）", len(batch))
            return []

        return result.get("mappings", [])

    # ------------------------------------------------------------------
    # 汇总与格式化
    # ------------------------------------------------------------------

    def _build_top_sectors(self, mappings: list[dict]) -> list[dict]:
        """
        将 news_industry_mapping 聚合为 top_sectors 格式。
        同一行业多条映射按加权 impact*confidence 累加，多数票决定情绪方向。
        """
        industry_agg: dict[str, dict] = defaultdict(
            lambda: {"bullish": 0.0, "bearish": 0.0, "neutral": 0.0, "reasons": [], "count": 0}
        )

        for m in mappings:
            name     = m.get("industry_name", "")
            sent     = m.get("sentiment", "neutral")
            impact   = float(m.get("impact_score") or 0.5)
            conf     = float(m.get("confidence")   or 0.5)
            weight   = impact * conf

            industry_agg[name][sent]  = industry_agg[name].get(sent, 0) + weight
            industry_agg[name]["count"] += 1
            if m.get("reason"):
                industry_agg[name]["reasons"].append(m["reason"])

        results: list[dict] = []
        for name, data in industry_agg.items():
            b      = data.get("bullish", 0)
            e      = data.get("bearish", 0)
            n_val  = data.get("neutral", 0)
            total  = b + e + n_val
            if total == 0:
                continue

            if b >= e and b >= n_val:
                direction = "bullish"
                score     = int(min(100, (b / total) * 85 + 15))
            elif e > b and e >= n_val:
                direction = "bearish"
                score     = int(min(100, (e / total) * 85 + 15))
            else:
                direction = "neutral"
                score     = 50

            reasons = list(dict.fromkeys(data["reasons"]))[:2]
            results.append({
                "sector":    name,
                "score":     score,
                "direction": direction,
                "reason":    "；".join(reasons),
                "news_count": data["count"],
            })

        # 排序：看多板块在前，按 score 降序
        results.sort(
            key=lambda x: (0 if x["direction"] == "bullish" else (2 if x["direction"] == "bearish" else 1), -x["score"])
        )
        return results[:self.top_n]

    def _calc_sentiment(
        self,
        pending: list[dict],
        mappings: list[dict],
        flow_score: dict,
    ) -> dict:
        """根据行业映射统计和流量分计算市场情绪"""
        bullish_w = sum(
            (m.get("impact_score") or 0.5) for m in mappings if m.get("sentiment") == "bullish"
        )
        bearish_w = sum(
            (m.get("impact_score") or 0.5) for m in mappings if m.get("sentiment") == "bearish"
        )
        total_w   = bullish_w + bearish_w + 0.001
        net_score = (bullish_w - bearish_w) / total_w   # -1 ~ +1
        flow      = flow_score.get("total_score", 50)

        if net_score > 0.4 and flow >= 55:
            sentiment_class = "极度乐观"
            flow_stage      = "流量高潮（谨慎追高）"
        elif net_score > 0.2:
            sentiment_class = "乐观"
            flow_stage      = "流量上升期"
        elif net_score > -0.2:
            sentiment_class = "中性"
            flow_stage      = "流量平稳期"
        elif net_score > -0.4:
            sentiment_class = "谨慎"
            flow_stage      = "流量低迷期"
        else:
            sentiment_class = "悲观"
            flow_stage      = "流量萎缩期"

        return {
            "sentiment_class": sentiment_class,
            "flow_stage":      flow_stage,
            "bullish_weight":  round(bullish_w, 2),
            "bearish_weight":  round(bearish_w, 2),
            "net_score":       round(net_score, 3),
        }

    def _build_summary(
        self,
        trade_date:    date,
        top_sectors:   list[dict],
        sentiment_info: dict,
        flow_score:    dict,
    ) -> str:
        bullish = [s for s in top_sectors if s["direction"] == "bullish"]
        bearish = [s for s in top_sectors if s["direction"] == "bearish"]

        bull_names = "、".join(s["sector"] for s in bullish[:5]) or "暂无明显利好板块"
        bear_names = "、".join(s["sector"] for s in bearish[:3]) or "无"

        lines = [
            f"## 新闻舆情分析（{trade_date}）\n",
            f"**市场热度**：{flow_score.get('level', '?')} "
            f"（综合得分 {flow_score.get('total_score', 0)}）",
            f"**情绪判断**：{sentiment_info.get('sentiment_class', '中性')}，"
            f"处于「{sentiment_info.get('flow_stage', '未知')}」阶段",
            f"**舆情利好板块**：{bull_names}",
            f"**舆情承压板块**：{bear_names}",
        ]

        if bullish:
            lines.append("\n**板块详情**：")
            for s in bullish[:5]:
                lines.append(f"- 🔴 {s['sector']}（影响新闻 {s['news_count']} 条）：{s['reason'][:60]}")
        if bearish:
            for s in bearish[:2]:
                lines.append(f"- 🔵 {s['sector']}（影响新闻 {s['news_count']} 条）：{s['reason'][:60]}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_flow_score(pending: list[dict]) -> dict:
        """从 news_raw.extra 字段提取流量分信息"""
        for news in pending:
            extra = news.get("extra") or {}
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {}
            if extra.get("flow_score") is not None:
                return {
                    "total_score":      extra.get("flow_score", 0),
                    "level":            extra.get("flow_level", "?"),
                    "active_platforms": extra.get("active_platforms", 0),
                }
        return {"total_score": 50, "level": "中", "active_platforms": 0}


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------

def _clamp(val, lo: float, hi: float) -> float | None:
    if val is None:
        return None
    try:
        v = float(val)
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return None


def _top_industries(mappings: list[dict], n: int = 10) -> list[dict]:
    """统计当天出现频次最高的行业"""
    from collections import Counter
    c = Counter(m["industry_name"] for m in mappings)
    return [{"industry": k, "count": v} for k, v in c.most_common(n)]
