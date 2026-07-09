"""
热度计算引擎 - 策略模式（Strategy Pattern）

支持两种策略：
  CapitalModeStrategy  - 资金模式（依赖主力净流入 / 上涨 / 涨停家数）
  HistoryModeStrategy  - 历史行情模式（仅依赖涨跌幅 + 成交额）

HeatEngine 根据当日数据质量自动选择策略（mode: auto），
也可在 heat_engine.mode 中强制指定。

外层业务只需调用：
    cap_score, lead_score, mode_used = engine.calculate_daily_scores(current_df, history_df)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 归一化工具
# ---------------------------------------------------------------------------

def _percentile_rank(series: pd.Series) -> pd.Series:
    """截面百分位排名：NaN 给 50，其余转换到 [0, 100]"""
    from scipy.stats import rankdata
    valid = series.notna()
    result = pd.Series(50.0, index=series.index)
    if valid.sum() < 2:
        return result
    ranks = rankdata(series[valid], method="average")
    pct = (ranks - 1) / (valid.sum() - 1) * 100
    result[valid] = pct
    return result.round(2)


def _minmax_norm(series: pd.Series, default: float = 50.0) -> pd.Series:
    """Min-Max 归一化到 [0, 100]"""
    vmin, vmax = series.min(), series.max()
    if pd.isna(vmin) or pd.isna(vmax) or vmin == vmax:
        return pd.Series(default, index=series.index)
    return ((series - vmin) / (vmax - vmin) * 100).fillna(default).round(2)


# ---------------------------------------------------------------------------
# 抽象策略基类
# ---------------------------------------------------------------------------

class BaseStrategy(ABC):
    @abstractmethod
    def mode_name(self) -> str: ...

    @abstractmethod
    def calculate_daily_scores(
        self,
        current_df: pd.DataFrame,
        history_df: pd.DataFrame,
        cont_cfg: dict,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        计算当日资金强度得分和领涨得分。

        Args:
            current_df:  今日数据 DataFrame（sector_name 为一列，含原始字段）
            history_df:  历史数据 DataFrame（不含今日，按 trade_date 升序，
                         至少包含 trade_date / sector_name / price_change_pct /
                         turnover_amount 四列）
            cont_cfg:    continuous_score 配置字典

        Returns:
            (daily_capital_score, daily_leader_score)
            两个与 current_df 同索引的 Series，值域 [0, 100]
        """
        ...


# ---------------------------------------------------------------------------
# 资金模式策略
# ---------------------------------------------------------------------------

class CapitalModeStrategy(BaseStrategy):
    """
    资金模式：依赖主力净流入占比、上涨家数占比、涨停占比。
    适用于能实时获取 THS summary 数据的交易日。
    """

    def mode_name(self) -> str:
        return "capital"

    def calculate_daily_scores(
        self,
        current_df: pd.DataFrame,
        history_df: pd.DataFrame,
        cont_cfg: dict,
    ) -> Tuple[pd.Series, pd.Series]:
        # ---- 连续资金流入分 ----
        capital_weights: dict[str, float] = cont_cfg.get(
            "capital_weights", {"main_net_inflow_pct": 1.0}
        )
        cap_score = pd.Series(0.0, index=current_df.index)
        total_cw = sum(capital_weights.values()) or 1.0
        for col, w in capital_weights.items():
            if col in current_df.columns:
                cap_score += _percentile_rank(
                    pd.to_numeric(current_df[col], errors="coerce")
                ) * (w / total_cw)

        # ---- 连续领涨分 ----
        leader_weights: dict[str, float] = cont_cfg.get(
            "leader_weights",
            {"price_change_pct": 0.40, "limit_up_ratio": 0.35, "rise_ratio": 0.25},
        )
        # 优先使用 calculator.py 预计算的 _* 中间列
        col_map = {
            "price_change_pct": "_price",
            "limit_up_ratio":   "_limit_up_ratio",
            "rise_ratio":       "_rise_ratio",
        }
        lead_score = pd.Series(0.0, index=current_df.index)
        total_lw = sum(leader_weights.values()) or 1.0
        for key, w in leader_weights.items():
            src = col_map.get(key, key)
            col = src if src in current_df.columns else key
            if col in current_df.columns:
                lead_score += _percentile_rank(
                    pd.to_numeric(current_df[col], errors="coerce")
                ) * (w / total_lw)

        return cap_score.round(2), lead_score.round(2)


# ---------------------------------------------------------------------------
# 历史行情模式策略
# ---------------------------------------------------------------------------

class HistoryModeStrategy(BaseStrategy):
    """
    历史行情模式：仅依赖涨跌幅 + 成交额，不需要主力资金 / 涨停家数等数据。
    适用于历史回填场景。

    连续资金流入分 = 成交额相对强度(40%) + 连续放量天数(40%) + 成交额历史分位(20%)
    连续领涨分     = 今日涨跌幅(30%)    + 连续上涨天数(40%) + 近N日累计涨幅(30%)
    """

    def __init__(self, history_cfg: dict):
        self.vol_ma_period        = int(history_cfg.get("volume_ma_period", 20))
        self.vol_pct_period       = int(history_cfg.get("volume_percentile_period", 120))
        self.rise_period          = int(history_cfg.get("rise_period", 10))
        self.max_consec           = int(history_cfg.get("max_consecutive_days", 10))

    def mode_name(self) -> str:
        return "history"

    def calculate_daily_scores(
        self,
        current_df: pd.DataFrame,
        history_df: pd.DataFrame,
        cont_cfg: dict,
    ) -> Tuple[pd.Series, pd.Series]:
        # 合并历史 + 今日（今日作为最后一行）
        cur_slice = pd.DataFrame({
            "trade_date":      current_df["trade_date"].values,
            "sector_name":     current_df["sector_name"].values,
            "price_change_pct": pd.to_numeric(
                current_df.get("price_change_pct", pd.Series(dtype=float)),
                errors="coerce"
            ).values,
            "turnover_amount": pd.to_numeric(
                current_df["turnover_amount"] if "turnover_amount" in current_df.columns
                else pd.Series(np.nan, index=current_df.index),
                errors="coerce"
            ).values,
        })

        hist_slice = pd.DataFrame()
        if not history_df.empty:
            need_cols = ["trade_date", "sector_name", "price_change_pct", "turnover_amount"]
            avail = [c for c in need_cols if c in history_df.columns]
            hist_slice = history_df[avail].copy()
            for c in need_cols:
                if c not in hist_slice.columns:
                    hist_slice[c] = np.nan

        all_df = pd.concat([hist_slice, cur_slice], ignore_index=True)
        all_df = all_df.sort_values(["trade_date", "sector_name"]).drop_duplicates(
            subset=["trade_date", "sector_name"], keep="last"
        )

        # Pivot 表：行=交易日, 列=板块
        vol_pivot = all_df.pivot(
            index="trade_date", columns="sector_name", values="turnover_amount"
        ).astype(float)
        pct_pivot = all_df.pivot(
            index="trade_date", columns="sector_name", values="price_change_pct"
        ).astype(float)

        # ----------------------------------------------------------------
        # 连续资金流入分三因子
        # ----------------------------------------------------------------

        # ① 成交额相对强度 = today / MA(past N days, excl. today)
        ma_excl_today = (
            vol_pivot.rolling(self.vol_ma_period + 1, min_periods=2)
            .mean()
            .shift(1)       # 不含今日的滚动均值
        )
        today_vol = vol_pivot.iloc[-1]
        vol_rel = (
            today_vol / ma_excl_today.iloc[-1].replace(0, np.nan)
        ).clip(0, 5).fillna(1.0)

        # ② 连续放量天数 / max_consec
        def _consec_inc(col: pd.Series) -> float:
            vals = col.dropna().values
            cnt = 0
            for i in range(len(vals) - 1, 0, -1):
                if vals[i] > vals[i - 1]:
                    cnt += 1
                else:
                    break
            return min(cnt, self.max_consec) / self.max_consec

        consec_vol = vol_pivot.apply(_consec_inc)

        # ③ 成交额历史分位（含今日）
        def _vol_pct(col: pd.Series) -> float:
            vals = col.dropna()
            if len(vals) < 2:
                return 0.5
            return float((vals <= vals.iloc[-1]).mean())

        vol_pct_score = vol_pivot.tail(self.vol_pct_period).apply(_vol_pct) * 100

        cap_combined = (
            _minmax_norm(vol_rel)    * 0.40
            + _minmax_norm(consec_vol) * 0.40
            + _minmax_norm(vol_pct_score) * 0.20
        )

        # ----------------------------------------------------------------
        # 连续领涨分三因子
        # ----------------------------------------------------------------

        # ① 今日涨跌幅
        today_pct = pct_pivot.iloc[-1]

        # ② 连续上涨天数 / max_consec
        def _consec_up(col: pd.Series) -> float:
            vals = col.fillna(0).values
            cnt = 0
            for v in reversed(vals):
                if v > 0:
                    cnt += 1
                else:
                    break
            return min(cnt, self.max_consec) / self.max_consec

        consec_up = pct_pivot.apply(_consec_up)

        # ③ 近 N 日累计涨幅（含今日）
        cum_ret = pct_pivot.tail(self.rise_period).sum(min_count=1).fillna(0)

        lead_combined = (
            _minmax_norm(today_pct)   * 0.30
            + _minmax_norm(consec_up) * 0.40
            + _minmax_norm(cum_ret)   * 0.30
        )

        # ----------------------------------------------------------------
        # 映射回 current_df 索引
        # ----------------------------------------------------------------
        cap_result  = current_df["sector_name"].map(cap_combined).fillna(50.0)
        lead_result = current_df["sector_name"].map(lead_combined).fillna(50.0)

        logger.debug(
            "[HistoryMode] vol_rel=%.2f~%.2f  consec_up_avg=%.1f天  "
            "cap=%.1f~%.1f  lead=%.1f~%.1f",
            vol_rel.min(), vol_rel.max(),
            consec_up.mean() * self.max_consec,
            cap_result.min(), cap_result.max(),
            lead_result.min(), lead_result.max(),
        )
        return cap_result.round(2), lead_result.round(2)


# ---------------------------------------------------------------------------
# 热度引擎（对外统一入口）
# ---------------------------------------------------------------------------

class HeatEngine:
    """
    热度计算引擎，根据数据质量自动选择计算策略。

    配置 mode:
      'auto'    — 按当日数据质量自动判断（默认）
      'capital' — 强制使用资金模式
      'history' — 强制使用历史行情模式
    """

    def __init__(self, mode: str, engine_cfg: dict, cont_cfg: dict):
        self._mode     = mode
        self._capital  = CapitalModeStrategy()
        self._history  = HistoryModeStrategy(engine_cfg.get("history", {}))
        self._cont_cfg = cont_cfg

    def _detect_mode(self, current_df: pd.DataFrame) -> str:
        """
        自动检测：若超过 50% 板块有有效的 main_net_inflow_pct 且 rise_count，
        则使用资金模式；否则使用历史行情模式。
        """
        n = max(len(current_df), 1)
        has_inflow = (
            "main_net_inflow_pct" in current_df.columns
            and current_df["main_net_inflow_pct"].notna().sum() / n > 0.5
        )
        has_rise = (
            "rise_count" in current_df.columns
            and pd.to_numeric(current_df["rise_count"], errors="coerce").notna().sum() / n > 0.5
        )
        return "capital" if (has_inflow and has_rise) else "history"

    def calculate_daily_scores(
        self,
        current_df: pd.DataFrame,
        history_df: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series, str]:
        """
        计算当日每个板块的资金强度分与领涨分。

        Args:
            current_df:  今日原始数据 DataFrame（含 _* 预计算中间列）
            history_df:  历史原始数据 DataFrame（不含今日，按 trade_date 升序）

        Returns:
            (daily_capital_score, daily_leader_score, mode_used)
            前两个与 current_df 同索引，值域 [0, 100]
        """
        mode = self._detect_mode(current_df) if self._mode == "auto" else self._mode
        strategy: BaseStrategy = self._capital if mode == "capital" else self._history
        cap, lead = strategy.calculate_daily_scores(current_df, history_df, self._cont_cfg)
        logger.info(
            "[HeatEngine] %s 模式  cap 均值=%.1f  lead 均值=%.1f",
            mode, cap.mean(), lead.mean(),
        )
        return cap, lead, mode
