"""
市场指数数据采集 Skill

采集以下指数的日线数据并存入 market_index_daily 表：
  - 上证指数  (sh000001)  A 股大盘
  - 深证成指  (sz399001)  A 股深市大盘
  - 创业板指  (sz399006)  成长板
  - 科创50    (sh000688)  科创板（科创综指代表）
  - 恒生指数  (HSI)       港股大盘
  - 恒生科技  (HSTECH)    港股科技

数据源：
  A 股 → AkShare `stock_zh_index_daily`（同花顺接口，稳定）
  港股 → AkShare `stock_hk_index_daily_sina`（新浪接口，稳定）
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 指数定义
# ---------------------------------------------------------------------------

INDICES: list[dict] = [
    {"code": "sh000001", "name": "上证指数",     "market": "cn"},
    {"code": "sz399001", "name": "深证成指",     "market": "cn"},
    {"code": "sz399006", "name": "创业板指",     "market": "cn"},
    {"code": "sh000688", "name": "科创50",       "market": "cn"},  # 科创综指代表指数
    {"code": "HSI",      "name": "恒生指数",     "market": "hk"},
    {"code": "HSTECH",   "name": "恒生科技指数", "market": "hk"},
]

_CODE_TO_META: dict[str, dict] = {idx["code"]: idx for idx in INDICES}


# ---------------------------------------------------------------------------
# 采集函数
# ---------------------------------------------------------------------------

def _fetch_cn_index(
    code: str,
    start_date: date | None = None,
    end_date:   date | None = None,
) -> list[dict]:
    """
    采集 A 股指数日线数据。
    code 格式：sh000001 / sz399001 等。
    返回标准化记录列表（含 change_pct）。
    """
    import akshare as ak
    import pandas as pd

    meta = _CODE_TO_META.get(code, {})
    name = meta.get("name", code)

    df = ak.stock_zh_index_daily(symbol=code)
    # 列：date, open, high, low, close, volume
    df["date"] = pd.to_datetime(df["date"]).dt.date

    if start_date:
        df = df[df["date"] >= start_date]
    if end_date:
        df = df[df["date"] <= end_date]

    if df.empty:
        logger.warning("[IndexSkill] %s（%s）无数据", name, code)
        return []

    df = df.sort_values("date").reset_index(drop=True)
    # 计算涨跌幅
    df["change_pct"] = df["close"].pct_change() * 100

    records = []
    for _, row in df.iterrows():
        records.append({
            "index_code": code,
            "index_name": name,
            "trade_date": row["date"],
            "open":       float(row["open"])   if row["open"]   == row["open"] else None,
            "high":       float(row["high"])   if row["high"]   == row["high"] else None,
            "low":        float(row["low"])    if row["low"]    == row["low"]  else None,
            "close":      float(row["close"]),
            "volume":     int(row["volume"])   if row["volume"] == row["volume"] else None,
            "amount":     None,                 # A 股接口无成交额
            "change_pct": round(float(row["change_pct"]), 4) if row["change_pct"] == row["change_pct"] else None,
        })
    return records


def _fetch_hk_index(
    code: str,
    start_date: date | None = None,
    end_date:   date | None = None,
) -> list[dict]:
    """
    采集港股指数日线数据（新浪接口）。
    code：HSI（恒生指数）/ HSTECH（恒生科技指数）等。
    """
    import akshare as ak
    import pandas as pd

    meta = _CODE_TO_META.get(code, {})
    name = meta.get("name", code)

    df = ak.stock_hk_index_daily_sina(symbol=code)
    # 列：date, open, high, low, close, volume, amount
    df["date"] = pd.to_datetime(df["date"]).dt.date

    if start_date:
        df = df[df["date"] >= start_date]
    if end_date:
        df = df[df["date"] <= end_date]

    if df.empty:
        logger.warning("[IndexSkill] %s（%s）无数据", name, code)
        return []

    df = df.sort_values("date").reset_index(drop=True)
    df["change_pct"] = df["close"].pct_change() * 100

    records = []
    for _, row in df.iterrows():
        records.append({
            "index_code": code,
            "index_name": name,
            "trade_date": row["date"],
            "open":       float(row["open"])   if row["open"]   == row["open"] else None,
            "high":       float(row["high"])   if row["high"]   == row["high"] else None,
            "low":        float(row["low"])    if row["low"]    == row["low"]  else None,
            "close":      float(row["close"]),
            "volume":     int(row["volume"])   if row.get("volume") == row.get("volume") else None,
            "amount":     float(row["amount"]) if row.get("amount") == row.get("amount") else None,
            "change_pct": round(float(row["change_pct"]), 4) if row["change_pct"] == row["change_pct"] else None,
        })
    return records


# ---------------------------------------------------------------------------
# IndexSkill 主类
# ---------------------------------------------------------------------------

class IndexSkill:
    """
    市场指数 Skill

    支持全量采集、增量更新（按最新日期自动跳过已有数据）和按指定范围回填。
    """

    def __init__(self, interval: float = 1.0):
        """
        interval: 每个指数采集之间的间隔秒数（避免触发限流）
        """
        self.interval = interval

    def fetch_one(
        self,
        code:       str,
        start_date: date | None = None,
        end_date:   date | None = None,
    ) -> list[dict]:
        """采集单个指数"""
        meta   = _CODE_TO_META.get(code)
        market = meta.get("market", "cn") if meta else "cn"
        name   = meta.get("name", code)   if meta else code

        logger.info("[IndexSkill] 采集 %s（%s） %s ~ %s", name, code, start_date, end_date)
        try:
            if market == "hk":
                return _fetch_hk_index(code, start_date, end_date)
            else:
                return _fetch_cn_index(code, start_date, end_date)
        except Exception as e:
            logger.error("[IndexSkill] %s 采集失败: %s", code, e)
            return []

    def fetch_all(
        self,
        start_date: date | None = None,
        end_date:   date | None = None,
    ) -> list[dict]:
        """采集所有指数，返回合并后的记录列表"""
        all_records: list[dict] = []
        for idx in INDICES:
            records = self.fetch_one(idx["code"], start_date, end_date)
            all_records.extend(records)
            logger.info("[IndexSkill] %s: %d 条", idx["name"], len(records))
            time.sleep(self.interval)
        return all_records

    def fetch_and_store(
        self,
        engine,
        start_date: date | None = None,
        end_date:   date | None = None,
        incremental: bool = True,
    ) -> int:
        """
        采集所有指数并写入 market_index_daily 表。

        incremental=True 时：
          对每个指数查询已有的最新日期，仅拉取缺失部分，减少 API 调用。

        返回总写入条数。
        """
        from skills.index_db import init_index_table, upsert_index_records, query_all_indices_latest_date

        init_index_table(engine)
        latest_dates = query_all_indices_latest_date(engine) if incremental else {}

        total = 0
        for idx in INDICES:
            code = idx["code"]

            # 增量模式：若 DB 中已有数据，从最新日期+1天开始拉取
            _start = start_date
            if incremental and code in latest_dates:
                from datetime import timedelta
                db_latest  = latest_dates[code]
                auto_start = db_latest + timedelta(days=1)
                if _start is None or auto_start > _start:
                    _start = auto_start
                if end_date and _start > end_date:
                    logger.info("[IndexSkill] %s 数据已是最新（%s），跳过", idx["name"], db_latest)
                    continue

            records = self.fetch_one(code, _start, end_date)
            if records:
                n = upsert_index_records(engine, records)
                total += n
                logger.info("[IndexSkill] %s 写入 %d 条", idx["name"], n)
            time.sleep(self.interval)

        logger.info("[IndexSkill] 全部完成，共写入 %d 条", total)
        return total

    def get_index_list(self) -> list[dict]:
        """返回支持的指数列表"""
        return [{"code": i["code"], "name": i["name"], "market": i["market"]} for i in INDICES]
