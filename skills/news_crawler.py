"""
22 平台新闻热点爬虫（Raw Data Layer）

职责：
  1. 从 orz.ai 采集 22 个平台的热点标题
  2. 调用 LLM 批量预处理（摘要、关键词、事件类型、实体提取）
  3. 写入 news_raw 表（status=pending，等待 NewsAgent 分析）

现有方法（向后兼容）：
  fetch_all / get_hot_topics / extract_finance_news / calculate_flow_score

新增方法：
  fetch_and_store(trade_date, engine)  — 一键采集 + LLM预处理 + 入库
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import date
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 平台配置
# ---------------------------------------------------------------------------

PLATFORMS: dict[str, dict] = {
    "weibo":       {"name": "微博热搜",   "category": "social",  "weight": 10},
    "douyin":      {"name": "抖音热点",   "category": "social",  "weight": 9},
    "zhihu":       {"name": "知乎热榜",   "category": "social",  "weight": 7},
    "bilibili":    {"name": "哔哩哔哩",   "category": "social",  "weight": 6},
    "xiaohongshu": {"name": "小红书",     "category": "social",  "weight": 7},
    "kuaishou":    {"name": "快手",       "category": "social",  "weight": 6},
    "tieba":       {"name": "百度贴吧",   "category": "social",  "weight": 5},
    "weixin":      {"name": "微信热点",   "category": "social",  "weight": 8},
    "baidu":       {"name": "百度热搜",   "category": "news",    "weight": 8},
    "jinritoutiao":{"name": "今日头条",   "category": "news",    "weight": 7},
    "tenxunwang":  {"name": "腾讯网",     "category": "news",    "weight": 6},
    "netease":     {"name": "网易新闻",   "category": "news",    "weight": 6},
    "ifeng":       {"name": "凤凰网",     "category": "news",    "weight": 5},
    "sina":        {"name": "新浪新闻",   "category": "news",    "weight": 6},
    "sina_finance":{"name": "新浪财经",   "category": "finance", "weight": 9},
    "eastmoney":   {"name": "东方财富",   "category": "finance", "weight": 9},
    "xueqiu":      {"name": "雪球",       "category": "finance", "weight": 8},
    "cls":         {"name": "财联社",     "category": "finance", "weight": 8},
    "wallstreetcn":{"name": "华尔街见闻", "category": "finance", "weight": 7},
    "tskr":        {"name": "36氪",       "category": "tech",    "weight": 6},
    "sspai":       {"name": "少数派",     "category": "tech",    "weight": 5},
    "juejin":      {"name": "掘金",       "category": "tech",    "weight": 5},
}

FINANCE_KEYWORDS = [
    "股市", "A股", "大盘", "板块", "行业", "概念", "涨停", "跌停",
    "主力", "资金", "基金", "ETF", "牛市", "熊市", "利好", "利空",
    "政策", "降准", "降息", "央行", "财政", "经济", "GDP", "PMI",
    "科技", "半导体", "芯片", "AI", "人工智能", "新能源", "光伏",
    "医药", "消费", "地产", "银行", "券商", "保险", "军工", "化工",
    "锂电", "储能", "汽车", "机器人", "算力", "大模型", "云计算",
]

CATEGORY_WEIGHTS = {
    "finance": 1.5,
    "news":    1.0,
    "social":  0.8,
    "tech":    0.6,
}

# 事件类型枚举（LLM 输出约束）
EVENT_TYPES = ["政策", "行业数据", "公司公告", "国际事件", "宏观经济", "产业新闻", "市场热点"]


class NewsCrawler:
    """
    22 平台热点爬虫 + LLM 预处理

    公开接口（向后兼容）：
        fetch_all(categories)           → dict
        get_hot_topics(data)            → list
        extract_finance_news(data)      → list
        calculate_flow_score(data)      → dict

    新增接口：
        fetch_and_store(trade_date, engine, categories, max_items, batch_size)
            → 采集 + LLM预处理 + 写入 news_raw，返回入库条数
    """

    BASE_URL = "https://orz.ai/api/v1/dailynews/"

    def __init__(
        self,
        timeout:    int   = 10,
        interval:   float = 0.3,
        llm_client        = None,   # 可选注入 LLMClient，用于 fetch_and_store
    ):
        self.timeout  = timeout
        self.interval = interval
        self._llm     = llm_client
        self.session  = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        })

    # ------------------------------------------------------------------
    # 向后兼容的公开接口
    # ------------------------------------------------------------------

    def fetch_platform(self, platform: str) -> dict[str, Any]:
        """获取单个平台热点"""
        cfg = PLATFORMS.get(platform, {})
        try:
            resp = self.session.get(
                self.BASE_URL,
                params={"platform": platform},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "platform":   platform,
                "name":       cfg.get("name", platform),
                "category":   cfg.get("category", "other"),
                "weight":     cfg.get("weight", 5),
                "items":      data.get("data", []) if isinstance(data, dict) else data,
                "success":    True,
                "fetch_time": time.strftime("%H:%M:%S"),
            }
        except Exception as e:
            logger.debug("平台 %s 获取失败: %s", platform, e)
            return {
                "platform": platform,
                "name":     cfg.get("name", platform),
                "category": cfg.get("category", "other"),
                "weight":   cfg.get("weight", 5),
                "items":    [],
                "success":  False,
                "error":    str(e),
            }

    def fetch_all(self, categories: list[str] | None = None) -> dict[str, Any]:
        """
        批量获取多平台热点。

        Args:
            categories: 限定类别，如 ["finance", "news"]；None = 全部

        Returns:
            { platform_code: {..., "items": [{title, rank, ...}]}, ... }
        """
        targets = [
            p for p, cfg in PLATFORMS.items()
            if categories is None or cfg["category"] in categories
        ]
        results: dict[str, Any] = {}
        for i, platform in enumerate(targets):
            results[platform] = self.fetch_platform(platform)
            if i < len(targets) - 1:
                time.sleep(self.interval)

        success = sum(1 for v in results.values() if v.get("success"))
        logger.info("新闻爬取完成：%d/%d 个平台成功", success, len(targets))
        return results

    def get_hot_topics(
        self,
        platforms_data: dict[str, Any],
        top_n: int = 20,
    ) -> list[dict[str, Any]]:
        """聚合全平台热门话题，按加权出现次数排名"""
        title_scores: dict[str, float] = {}
        title_sources: dict[str, list[str]] = {}

        for pdata in platforms_data.values():
            if not pdata.get("success"):
                continue
            w        = float(pdata.get("weight", 5))
            cat_mult = CATEGORY_WEIGHTS.get(pdata.get("category", "other"), 1.0)
            name     = pdata.get("name", pdata.get("platform", ""))
            for item in pdata.get("items", [])[:50]:
                title = _extract_title(item)
                if not title:
                    continue
                rank_bonus = 1.0 + (50 - min(item.get("rank", 50), 50)) / 50
                score = w * cat_mult * rank_bonus
                title_scores[title]  = title_scores.get(title, 0) + score
                title_sources.setdefault(title, []).append(name)

        ranked = sorted(title_scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
        return [
            {
                "title":        title,
                "score":        round(score, 1),
                "sources":      title_sources.get(title, []),
                "source_count": len(title_sources.get(title, [])),
            }
            for title, score in ranked
        ]

    def extract_finance_news(
        self,
        platforms_data: dict[str, Any],
        extra_keywords: list[str] | None = None,
        top_n: int = 30,
    ) -> list[dict[str, Any]]:
        """从所有平台过滤出财经/板块相关新闻"""
        keywords = FINANCE_KEYWORDS + (extra_keywords or [])
        scored: list[dict[str, Any]] = []

        for pcode, pdata in platforms_data.items():
            if not pdata.get("success"):
                continue
            cat  = pdata.get("category", "other")
            w    = float(pdata.get("weight", 5))
            name = pdata.get("name", "")
            for item in pdata.get("items", [])[:50]:
                title = _extract_title(item)
                if not title:
                    continue
                is_finance_platform = (cat == "finance")
                has_keyword = any(kw in title for kw in keywords)
                if not (is_finance_platform or has_keyword):
                    continue
                kw_count = sum(1 for kw in keywords if kw in title)
                cat_mult = CATEGORY_WEIGHTS.get(cat, 1.0)
                score    = w * cat_mult * (1 + kw_count * 0.3)
                scored.append({
                    "title":    title,
                    "platform": name,
                    "source":   pcode,
                    "category": cat,
                    "rank":     item.get("rank", 99),
                    "url":      item.get("url", item.get("href", "")),
                    "score":    round(score, 1),
                })

        seen: set[str] = set()
        deduped: list[dict] = []
        for item in sorted(scored, key=lambda x: x["score"], reverse=True):
            if item["title"] not in seen:
                seen.add(item["title"])
                deduped.append(item)
            if len(deduped) >= top_n:
                break
        return deduped

    def calculate_flow_score(self, platforms_data: dict[str, Any]) -> dict[str, Any]:
        """计算全网流量热度综合得分（0~100）"""
        cat_raw: dict[str, float] = {}
        cat_max: dict[str, float] = {}

        for pdata in platforms_data.values():
            cat = pdata.get("category", "other")
            w   = float(pdata.get("weight", 5))
            max_score = w * CATEGORY_WEIGHTS.get(cat, 1.0)
            cat_max[cat] = cat_max.get(cat, 0) + max_score
            if pdata.get("success") and pdata.get("items"):
                cat_raw[cat] = cat_raw.get(cat, 0) + max_score

        if not cat_max:
            return {"total_score": 0, "level": "极低", "category_scores": {}, "active_platforms": 0}

        total_w    = sum(cat_max.values())
        total_s    = sum(cat_raw.get(cat, 0) for cat in cat_max)
        raw_ratio  = total_s / total_w if total_w else 0
        total_score = min(int(raw_ratio * 100), 100)

        if total_score >= 80:   level = "极高"
        elif total_score >= 60: level = "高"
        elif total_score >= 40: level = "中"
        elif total_score >= 20: level = "低"
        else:                   level = "极低"

        return {
            "total_score":     total_score,
            "level":           level,
            "category_scores": {
                cat: round(cat_raw.get(cat, 0) / cat_max.get(cat, 1) * 100, 1)
                for cat in cat_max
            },
            "active_platforms": sum(1 for v in platforms_data.values() if v.get("success")),
        }

    # ------------------------------------------------------------------
    # 新接口：采集 + LLM预处理 + 写库
    # ------------------------------------------------------------------

    def fetch_and_store(
        self,
        trade_date:  str | date,
        engine,
        categories:  list[str] | None = None,
        max_items:   int = 40,
        batch_size:  int = 10,
    ) -> int:
        """
        一键采集 + LLM预处理 + 写入 news_raw。

        流程：
          1. 采集多平台热点
          2. 合并去重，保留 max_items 条最高权重新闻
          3. 批量调用 LLM 补充摘要/关键词/事件类型/实体
          4. 写入 news_raw（hash 冲突自动跳过，状态默认 pending）

        Returns:
            实际新增条数
        """
        from skills.news_db import insert_news_raw

        # 1. 采集
        platforms_data = self.fetch_all(categories=categories)
        flow_score     = self.calculate_flow_score(platforms_data)
        finance_news   = self.extract_finance_news(platforms_data, top_n=max_items)
        hot_topics     = self.get_hot_topics(platforms_data, top_n=max_items // 2)

        # 2. 合并去重
        merged = self._merge_items(finance_news, hot_topics)[:max_items]
        if not merged:
            logger.warning("[NewsCrawler] 未采集到任何新闻")
            return 0

        logger.info("[NewsCrawler] 合并后 %d 条新闻，开始 LLM 预处理…", len(merged))

        # 3. LLM 批量预处理（无 LLM 则跳过，摘要留空）
        if self._llm is not None:
            merged = self._preprocess_all(merged, batch_size=batch_size)
        else:
            logger.warning("[NewsCrawler] 未配置 LLM 客户端，跳过摘要/关键词提取")

        # 4. 构造 DB 记录（将流量分写入 extra 字段）
        td      = str(trade_date)
        records = []
        for item in merged:
            records.append({
                "title":      item["title"],
                "source":     item.get("source", ""),
                "platform":   item.get("platform", ""),
                "url":        item.get("url", ""),
                "summary":    item.get("summary"),
                "keywords":   item.get("keywords"),
                "event_type": item.get("event_type"),
                "entities":   item.get("entities"),
                "trade_date": td,
                "status":     "pending",
                "extra": {
                    "flow_score":      flow_score.get("total_score", 0),
                    "flow_level":      flow_score.get("level", "?"),
                    "active_platforms": flow_score.get("active_platforms", 0),
                    "item_score":      item.get("score", 0),
                    "item_rank":       item.get("rank"),
                },
            })

        return insert_news_raw(engine, records)

    # ------------------------------------------------------------------
    # LLM 预处理内部方法
    # ------------------------------------------------------------------

    def _merge_items(
        self,
        finance_news: list[dict],
        hot_topics:   list[dict],
    ) -> list[dict]:
        """
        合并财经新闻与全站热点，按 score 降序，去重。
        保留 finance_news 中的原始字段（含 source/platform/url）。
        """
        seen:   set[str]   = set()
        merged: list[dict] = []

        for item in finance_news:
            t = item["title"]
            if t not in seen:
                seen.add(t)
                merged.append(item)

        for topic in hot_topics:
            t = topic["title"]
            if t not in seen:
                seen.add(t)
                merged.append({
                    "title":    t,
                    "source":   "hot_topic",
                    "platform": "、".join(topic.get("sources", [])[:2]),
                    "url":      "",
                    "score":    topic.get("score", 0),
                })

        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged

    def _preprocess_all(self, items: list[dict], batch_size: int = 10) -> list[dict]:
        """分批调用 LLM，逐批补充预处理字段"""
        for i in range(0, len(items), batch_size):
            batch  = items[i:i + batch_size]
            result = self._llm_preprocess_batch(batch)
            for j, item in enumerate(batch):
                if j < len(result):
                    item.update(result[j])
        return items

    def _llm_preprocess_batch(self, batch: list[dict]) -> list[dict]:
        """
        对一批新闻标题调用 LLM，返回预处理结果列表（与 batch 等长）。
        失败时返回全空列表。
        """
        if not batch or self._llm is None:
            return [{}] * len(batch)

        event_types_str = "、".join(EVENT_TYPES)
        titles_text = "\n".join(f"{i+1}. {item['title']}" for i, item in enumerate(batch))

        prompt = (
            f"请对以下 {len(batch)} 条新闻标题逐条分析，输出 JSON 数组。\n\n"
            f"=== 新闻标题 ===\n{titles_text}\n\n"
            f"每条输出格式（保持顺序与上述标题一一对应）：\n"
            f'{{"index":1,"summary":"100~200字，扩展背景，适合投资分析","keywords":["词1","词2"],'
            f'"event_type":"从[{event_types_str}]中选一个","entities":["实体1","实体2"]}}\n\n'
            f"只输出纯 JSON 数组，不要其他文字：\n"
            f'[{{"index":1,...}},{{"index":2,...}}]'
        )

        try:
            raw = self._llm.chat_json(
                [
                    {"role": "system", "content": "你是财经新闻分析师，只输出纯 JSON 数组。"},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=2000,
            )
        except Exception as e:
            logger.warning("[NewsCrawler] LLM 预处理调用失败: %s", e)
            return [{}] * len(batch)

        # 归一化为 list[dict]：LLM 可能返回数组、单个对象、或包了一层的字典
        if isinstance(raw, dict):
            if "index" in raw:
                # 单个分析对象（未包数组）
                raw = [raw]
            else:
                # 外层包了一层，如 {"result": [...]} / {"data": {...}}
                inner = next((v for v in raw.values() if isinstance(v, (list, dict))), None)
                raw = [inner] if isinstance(inner, dict) else (inner if isinstance(inner, list) else [])
        if not isinstance(raw, list):
            return [{}] * len(batch)

        enriched: list[dict] = [{}] * len(batch)
        for item in raw:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("index", 0)) - 1
            if 0 <= idx < len(batch):
                enriched[idx] = {
                    "summary":    item.get("summary", ""),
                    "keywords":   item.get("keywords", []),
                    "event_type": item.get("event_type", ""),
                    "entities":   item.get("entities",  []),
                }
        return enriched


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _extract_title(item: Any) -> str:
    """从热点条目（dict 或 str）中提取标题"""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("title", "name", "text", "content", "keyword"):
            v = item.get(key)
            if v and isinstance(v, str):
                return v.strip()
    return ""
