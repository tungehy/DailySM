"""
行情数据采集模块（同花顺 THS 数据源）

使用同花顺接口替代东方财富 push2 接口，避免网络过滤问题。
AkShare 接口：
  - ak.stock_board_industry_summary_ths()   行业板块全量快照（涨跌幅/净流入/上下涨家数）
  - ak.stock_board_industry_name_ths()      板块列表+代码（用于涨停统计）
  - ak.stock_zt_pool_em(date)               涨停池（东方财富，较稳定）
  - ak.stock_board_industry_cons_em(symbol) 板块成分股（东方财富，用于归属涨停）
"""
from __future__ import annotations

from datetime import date
from typing import Any
import logging
import time

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _retry(fn, max_retries: int = 3, interval: float = 2.0):
    """带重试的函数调用"""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            logger.warning("接口调用失败（第 %d 次）: %s，%.1fs 后重试…",
                           attempt + 1, e, interval)
            time.sleep(interval)


# ---------------------------------------------------------------------------
# 主采集类
# ---------------------------------------------------------------------------

class SectorDataCollector:
    """采集指定交易日的行业板块全量指标（THS 数据源）"""

    def __init__(self, request_interval: float = 1.5, max_retries: int = 3):
        self.request_interval = request_interval
        self.max_retries = max_retries
        self._sector_stocks_cache: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def collect_range(self, start: date, end: date) -> list[dict]:
        """
        批量采集 [start, end] 日期段所有交易日的历史数据。
        使用 THS 历史指数接口（每个板块一次调用，返回整段数据），效率高。
        只能获取 涨跌幅 + 成交额；净流入 / 涨停 / 上涨下跌家数由当日 summary 提供，
        历史回填时置 NULL（不可得）。
        """
        import akshare as ak
        from datetime import timedelta

        end_str = end.strftime("%Y%m%d")
        # 向前多拉取 10 个自然日，保证 pct_change 第一天有前收盘价
        fetch_start     = start - timedelta(days=10)
        fetch_start_str = fetch_start.strftime("%Y%m%d")
        logger.info("批量历史回填 %s → %s（拉取从 %s 起含缓冲）…",
                    start.strftime("%Y%m%d"), end_str, fetch_start_str)

        # 1. 获取板块列表
        df_names = _retry(lambda: ak.stock_board_industry_name_ths(),
                          self.max_retries, self.request_interval)
        sector_list = df_names["name"].tolist()
        logger.info("共 %d 个板块，开始拉取历史指数…", len(sector_list))

        # 2. 每个板块拉取 OHLCV（含缓冲期）
        sector_dfs: dict[str, pd.DataFrame] = {}
        for i, sector in enumerate(sector_list):
            try:
                df = _retry(
                    lambda s=sector: ak.stock_board_industry_index_ths(
                        symbol=s, start_date=fetch_start_str, end_date=end_str
                    ),
                    self.max_retries, self.request_interval,
                )
                if df is not None and not df.empty and "日期" in df.columns and "收盘价" in df.columns:
                    df["日期"] = pd.to_datetime(df["日期"]).dt.date
                    df = df.set_index("日期").sort_index()
                    sector_dfs[sector] = df
            except Exception as e:
                logger.warning("板块 %s 历史数据获取失败: %s", sector, e)
            if (i + 1) % 10 == 0:
                logger.info("  已完成 %d/%d", i + 1, len(sector_list))
                time.sleep(self.request_interval * 0.5)

        logger.info("OHLCV 拉取完成，计算涨跌幅…")

        # 3. 收集目标区间内的交易日（过滤周末和缓冲期）
        all_dates: set[date] = set()
        for df in sector_dfs.values():
            all_dates.update(df.index)
        all_dates = {
            d for d in all_dates
            if start <= d <= end
            and d.weekday() < 5        # 过滤周六(5)、周日(6)
        }
        logger.info("时间段内共 %d 个交易日（已过滤周末）", len(all_dates))

        # 4. 每个板块计算日度涨跌幅（close/prev_close - 1）
        records: list[dict] = []
        for sector, df in sector_dfs.items():
            close    = df["收盘价"].astype(float)
            pct      = close.pct_change() * 100   # 涨跌幅
            # 成交额原始单位为元，除以 10000 转换为万元（与实时采集保持一致）
            turn_amt = df["成交额"].astype(float) / 10000 if "成交额" in df.columns \
                       else pd.Series(dtype=float)

            for d in sorted(all_dates):
                if d not in df.index:
                    continue
                records.append({
                    "trade_date":          d,
                    "sector_name":         sector,
                    "price_change_pct":    _safe_float(pct.get(d)),
                    "main_net_inflow":     None,
                    "main_net_inflow_pct": None,
                    "turnover_amount":     _safe_float(turn_amt.get(d)),
                    "rise_count":          None,
                    "fall_count":          None,
                    "limit_up_count":      None,
                    "total_count":         None,
                })

        logger.info("历史回填记录: %d 条（%d 板块 × %d 交易日）",
                    len(records), len(sector_dfs), len(all_dates))
        return records

    def collect(self, trade_date: date) -> list[dict]:
        """
        采集 trade_date 当天的行业板块数据。
        注意：THS summary 接口返回当前最新数据（无法指定历史日期），
        用于 run 子命令（每日采集当天数据）。
        backfill 也调用此接口，但写入时以传入的 trade_date 为准。
        """
        date_str = trade_date.strftime("%Y%m%d")
        logger.info("开始采集 %s 行业板块数据（THS）…", date_str)

        # Step1: THS 行业板块汇总（涨跌幅 + 净流入 + 上涨/下跌家数）
        df_summary = self._fetch_ths_summary()
        if df_summary.empty:
            logger.error("THS 行情数据为空，终止采集")
            return []
        logger.info("THS 行情数据: %d 个板块", len(df_summary))
        time.sleep(self.request_interval)

        # Step2: 涨停池 → 按板块统计涨停家数
        limit_up_map = self._fetch_limit_up_by_sector(date_str, df_summary)
        logger.info("涨停数据: %d 个板块有涨停", len(limit_up_map))

        # Step3: 组装记录
        records = self._build_records(trade_date, df_summary, limit_up_map)
        logger.info("采集完成: %d 条记录", len(records))
        return records

    # ------------------------------------------------------------------
    # Step1  THS 行业板块汇总
    # ------------------------------------------------------------------

    def _fetch_ths_summary(self) -> pd.DataFrame:
        """
        同花顺行业板块汇总，返回标准化 DataFrame：
          sector_name, price_change_pct, main_net_inflow,
          rise_count, fall_count
        """
        import akshare as ak

        df = _retry(lambda: ak.stock_board_industry_summary_ths(),
                    self.max_retries, self.request_interval)

        # 列名映射（THS 汇总接口列名固定）
        rename_map = {
            "板块":     "sector_name",
            "涨跌幅":   "price_change_pct",
            "净流入":   "main_net_inflow",   # 单位：亿元
            "上涨家数": "rise_count",
            "下跌家数": "fall_count",
            "总成交额": "turnover_amount",    # 单位：亿元（备用）
        }
        df = df.rename(columns=rename_map)

        for col in ("price_change_pct",):
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace("%", ""), errors="coerce"
                )
        for col in ("main_net_inflow", "turnover_amount"):
            if col in df.columns:
                # THS 净流入单位为亿元，转换为万元（与原始设计保持一致）
                df[col] = pd.to_numeric(df[col], errors="coerce") * 10000
        for col in ("rise_count", "fall_count"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

        keep = [c for c in ("sector_name", "price_change_pct", "main_net_inflow",
                             "rise_count", "fall_count", "turnover_amount")
                if c in df.columns]
        return df[keep].copy()

    # ------------------------------------------------------------------
    # Step2  涨停统计
    # ------------------------------------------------------------------

    def _fetch_limit_up_by_sector(
        self,
        date_str: str,
        df_summary: pd.DataFrame,
    ) -> dict[str, int]:
        """
        从涨停池的「所属行业」列直接统计各板块涨停数。
        不需要逐板块查成分股，速度快且不依赖 push2 接口。
        """
        import akshare as ak

        try:
            df_zt = _retry(lambda: ak.stock_zt_pool_em(date=date_str),
                           self.max_retries, self.request_interval)
        except Exception as e:
            logger.warning("涨停池接口异常: %s，涨停数据置 0", e)
            return {}

        if df_zt is None or df_zt.empty:
            return {}

        industry_col = next((c for c in df_zt.columns if "行业" in c), None)
        if not industry_col:
            logger.warning("涨停池无「所属行业」列，涨停数据置 0")
            return {}

        total = len(df_zt)
        counts = df_zt[industry_col].value_counts().to_dict()
        logger.info("今日涨停股: %d 只，覆盖 %d 个行业", total, len(counts))
        return {str(k): int(v) for k, v in counts.items()}

    # ------------------------------------------------------------------
    # Step3  组装记录
    # ------------------------------------------------------------------

    def _build_records(
        self,
        trade_date: date,
        df: pd.DataFrame,
        limit_up_map: dict[str, int],
    ) -> list[dict]:
        df = df.copy()
        df["limit_up_count"] = df["sector_name"].map(limit_up_map).fillna(0).astype(int)
        df["total_count"] = (
            pd.to_numeric(df.get("rise_count", 0), errors="coerce").fillna(0)
            + pd.to_numeric(df.get("fall_count", 0), errors="coerce").fillna(0)
        ).astype(int)

        # main_net_inflow_pct：用净流入/总成交额 * 100 近似（THS 未直接提供）
        if "turnover_amount" in df.columns:
            df["main_net_inflow_pct"] = (
                pd.to_numeric(df["main_net_inflow"], errors="coerce")
                / pd.to_numeric(df["turnover_amount"], errors="coerce").replace(0, float("nan"))
                * 100
            ).round(4)
        else:
            df["main_net_inflow_pct"] = None

        records = []
        for _, row in df.iterrows():
            records.append({
                "trade_date":          trade_date,
                "sector_name":         str(row.get("sector_name", "")),
                "price_change_pct":    _safe_float(row.get("price_change_pct")),
                "main_net_inflow":     _safe_float(row.get("main_net_inflow")),
                "main_net_inflow_pct": _safe_float(row.get("main_net_inflow_pct")),
                "turnover_amount":     _safe_float(row.get("turnover_amount")),
                "rise_count":          _safe_int(row.get("rise_count")),
                "fall_count":          _safe_int(row.get("fall_count")),
                "limit_up_count":      _safe_int(row.get("limit_up_count")),
                "total_count":         _safe_int(row.get("total_count")),
            })
        return records


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None and str(v) not in ("nan", "None", "") else None
    except (ValueError, TypeError):
        return None


def _safe_int(v: Any) -> int:
    try:
        return int(float(v)) if v is not None and str(v) not in ("nan", "None", "") else 0
    except (ValueError, TypeError):
        return 0
