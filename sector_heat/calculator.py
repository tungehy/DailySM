"""
热度指数计算模块

将原始指标转换为 0-100 的综合热度评分，同时计算两项长期动量指标：
  - 连续资金流入分（Continuous Capital Score）
  - 连续领涨分（Continuous Leadership Score）

两项长期指标均使用「指数衰减记忆」模型：
  score_today = score_yesterday × decay_factor + daily_score_today

daily_score 由 HeatEngine 通过策略模式（Strategy Pattern）计算：
  - CapitalModeStrategy：依赖主力净流入 / 上涨 / 涨停家数（实时数据）
  - HistoryModeStrategy：仅依赖涨跌幅 + 成交额（历史回填数据）
  HeatEngine 在 mode=auto 时自动根据当日数据质量选择策略。

综合热度公式（权重来自配置）：
  heat = w_price      * price_score
       + w_fund       * fund_score
       + w_limit_up   * limit_up_score
       + w_rise_ratio * rise_ratio_score
       + w_cap        * continuous_capital_score_normalized
       + w_leader     * continuous_leader_score_normalized
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
import logging

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_PATH  = _PROJECT_ROOT / "config" / "config.yaml"


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def _load_cfg() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("sector_heat", {})


def _load_weights() -> dict[str, float]:
    return _load_cfg()["heat_index"]["weights"]


def _load_normalization() -> str:
    return _load_cfg()["heat_index"].get("normalization", "percentile")


def _load_continuous_cfg() -> dict:
    return _load_cfg().get("continuous_score", {})


def _build_engine():
    """根据配置文件构建 HeatEngine 实例"""
    from sector_heat.engine import HeatEngine
    cfg = _load_cfg()
    engine_cfg = cfg.get("heat_engine", {})
    cont_cfg   = cfg.get("continuous_score", {})
    mode       = engine_cfg.get("mode", "auto")
    return HeatEngine(mode, engine_cfg, cont_cfg)


# ---------------------------------------------------------------------------
# 公开 API：日度连续得分计算
# ---------------------------------------------------------------------------

def calculate_daily_capital_score(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """
    计算当日资金强度得分（0~100）。
    已委托给 CapitalModeStrategy，此函数保留以兼容外部调用。
    """
    from sector_heat.engine import CapitalModeStrategy
    cap, _ = CapitalModeStrategy().calculate_daily_scores(df, pd.DataFrame(), cfg)
    return cap


def calculate_daily_leader_score(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """
    计算当日领涨得分（0~100）。
    已委托给 CapitalModeStrategy，此函数保留以兼容外部调用。
    """
    from sector_heat.engine import CapitalModeStrategy
    _, lead = CapitalModeStrategy().calculate_daily_scores(df, pd.DataFrame(), cfg)
    return lead


def update_continuous_capital_score(
    sector_names: list[str],
    daily_scores: dict[str, float],
    prev_scores: dict[str, float],
    decay_factor: float,
    threshold: float,
    max_score: float,
) -> dict[str, float]:
    """
    使用指数衰减记忆模型更新连续资金流入分。

    score_today = score_yesterday × decay_factor + effective_daily_score

    Args:
        sector_names: 本日所有板块名称列表
        daily_scores: {sector: daily_capital_score}
        prev_scores:  {sector: prev_continuous_capital_score}（无历史则传 {}）
        decay_factor: 衰减系数（默认 0.85）
        threshold:    daily_score 低于此值则贡献置 0
        max_score:    得分上限（防无限增长）

    Returns:
        {sector: new_continuous_capital_score}
    """
    result: dict[str, float] = {}
    for name in sector_names:
        prev    = prev_scores.get(name, 0.0) or 0.0
        daily   = daily_scores.get(name, 0.0) or 0.0
        contrib = daily if (threshold <= 0 or daily >= threshold) else 0.0
        new_val = prev * decay_factor + contrib
        result[name] = min(new_val, max_score)
    return result


def update_sort_score(
    sector_names: list[str],
    heat_scores: dict[str, float],
    prev_sort_scores: dict[str, float],
    decay_factor: float,
) -> dict[str, float]:
    """
    更新排序分（用于热力图行业排序，与 heat_score 颜色完全分离）。

    sort_score_today = sort_score_yesterday × decay_factor + today_heat_score

    首次运行（prev 为空）时：sort_score = today_heat_score。

    Args:
        sector_names:    本日所有板块名称
        heat_scores:     {sector: heat_score}（0-100）
        prev_sort_scores:{sector: prev_sort_score}（无历史则传 {}）
        decay_factor:    衰减系数（默认 0.95）

    Returns:
        {sector: new_sort_score}
    """
    result: dict[str, float] = {}
    for name in sector_names:
        heat  = heat_scores.get(name, 0.0) or 0.0
        prev  = prev_sort_scores.get(name)
        # 首次运行：没有历史记录 → 直接用当天热度初始化
        new_val = (prev * decay_factor + heat) if prev is not None else heat
        result[name] = round(new_val, 4)
    return result


def update_continuous_leader_score(
    sector_names: list[str],
    daily_scores: dict[str, float],
    prev_scores: dict[str, float],
    decay_factor: float,
    threshold: float,
    max_score: float,
) -> dict[str, float]:
    """
    使用指数衰减记忆模型更新连续领涨分。
    参数含义与 update_continuous_capital_score 相同。
    """
    result: dict[str, float] = {}
    for name in sector_names:
        prev    = prev_scores.get(name, 0.0) or 0.0
        daily   = daily_scores.get(name, 0.0) or 0.0
        contrib = daily if (threshold <= 0 or daily >= threshold) else 0.0
        new_val = prev * decay_factor + contrib
        result[name] = min(new_val, max_score)
    return result


# ---------------------------------------------------------------------------
# 主计算入口
# ---------------------------------------------------------------------------

def compute_heat_for_date(
    raw_rows: list[dict],
    prev_continuous_scores: dict[str, dict] | None = None,
    prev_sort_scores: dict[str, float] | None = None,
    history_rows: list[dict] | None = None,
) -> list[dict]:
    """
    对单日的原始指标列表计算热度评分（含长期动量指标与排序分）。

    Args:
        raw_rows:               同一 trade_date 的 sector_daily_raw 记录列表
        prev_continuous_scores: 前一交易日的连续得分字典：
            {sector_name: {"continuous_capital_score": float,
                           "continuous_leader_score":  float}}
        prev_sort_scores:       前一交易日的 sort_score 字典：
            {sector_name: float}
            首次计算时传 None 或 {}（直接用 heat_score 初始化）
        history_rows:           HistoryModeStrategy 使用的历史原始数据列表
            （不含今日，按日期升序）；传 None 时 HistoryModeStrategy 无历史可用

    Returns:
        sector_heat_index 记录列表，每条包含：
          heat_score, price_score, fund_score,
          limit_up_score, rise_ratio_score,
          daily_capital_score, daily_leader_score,
          continuous_capital_score, continuous_leader_score,
          sort_score
    """
    if not raw_rows:
        return []

    weights   = _load_weights()
    norm      = _load_normalization()
    cont_cfg  = _load_continuous_cfg()
    df        = pd.DataFrame(raw_rows)
    trade_date = df["trade_date"].iloc[0]

    if prev_continuous_scores is None:
        prev_continuous_scores = {}
    if prev_sort_scores is None:
        prev_sort_scores = {}

    # ----------------------------------------------------------------
    # Step 1：构造短期子指标原始值
    # ----------------------------------------------------------------
    df["_price"] = pd.to_numeric(df["price_change_pct"], errors="coerce")
    df["_fund"]  = pd.to_numeric(df["main_net_inflow_pct"], errors="coerce")

    total = pd.to_numeric(df["total_count"], errors="coerce").replace(0, np.nan)
    df["_limit_up_ratio"] = pd.to_numeric(df["limit_up_count"], errors="coerce") / total * 100
    df["_rise_ratio"]     = pd.to_numeric(df["rise_count"],     errors="coerce") / total * 100

    # ----------------------------------------------------------------
    # Step 2：短期子指标归一化（0-100）
    # ----------------------------------------------------------------
    short_cols = ["_price", "_fund", "_limit_up_ratio", "_rise_ratio"]
    if norm == "percentile":
        for col in short_cols:
            df[col + "_score"] = _percentile_rank(df[col])
    else:
        for col in short_cols:
            df[col + "_score"] = _minmax_norm(df[col])

    # ----------------------------------------------------------------
    # Step 3：通过 HeatEngine 计算 daily_capital_score / daily_leader_score
    #         HeatEngine 自动选择 CapitalMode 或 HistoryMode 策略
    # ----------------------------------------------------------------
    engine      = _build_engine()
    history_df  = pd.DataFrame(history_rows) if history_rows else pd.DataFrame()
    cap_scores, lead_scores, _mode_used = engine.calculate_daily_scores(df, history_df)
    df["daily_capital_score"] = cap_scores.values
    df["daily_leader_score"]  = lead_scores.values

    # ----------------------------------------------------------------
    # Step 4：更新连续得分（指数衰减模型）
    # ----------------------------------------------------------------
    decay       = float(cont_cfg.get("decay_factor", 0.85))
    cap_thresh  = float(cont_cfg.get("capital_threshold", 30))
    lead_thresh = float(cont_cfg.get("leader_threshold", 30))
    max_score   = float(cont_cfg.get("max_score", 1000.0))

    sector_names = df["sector_name"].tolist()

    daily_cap_map  = dict(zip(df["sector_name"], df["daily_capital_score"]))
    daily_lead_map = dict(zip(df["sector_name"], df["daily_leader_score"]))

    prev_cap  = {k: v.get("continuous_capital_score", 0.0)
                 for k, v in prev_continuous_scores.items()}
    prev_lead = {k: v.get("continuous_leader_score", 0.0)
                 for k, v in prev_continuous_scores.items()}

    new_cap  = update_continuous_capital_score(
        sector_names, daily_cap_map, prev_cap, decay, cap_thresh, max_score)
    new_lead = update_continuous_leader_score(
        sector_names, daily_lead_map, prev_lead, decay, lead_thresh, max_score)

    df["continuous_capital_score"] = df["sector_name"].map(new_cap)
    df["continuous_leader_score"]  = df["sector_name"].map(new_lead)

    # ----------------------------------------------------------------
    # Step 5：连续得分截面归一化（0-100）用于合入 heat_score
    # ----------------------------------------------------------------
    df["_cont_cap_score"]  = _minmax_norm(df["continuous_capital_score"])
    df["_cont_lead_score"] = _minmax_norm(df["continuous_leader_score"])

    # ----------------------------------------------------------------
    # Step 6：加权求和 → heat_score
    # ----------------------------------------------------------------
    w = weights
    df["heat_score"] = (
        df["_price_score"]            * w.get("price_change_pct",    0.15)
        + df["_fund_score"]           * w.get("main_net_inflow_pct", 0.18)
        + df["_limit_up_ratio_score"] * w.get("limit_up_ratio",      0.12)
        + df["_rise_ratio_score"]     * w.get("rise_ratio",          0.05)
        + df["_cont_cap_score"]       * w.get("continuous_capital",  0.25)
        + df["_cont_lead_score"]      * w.get("continuous_leader",   0.25)
    ).clip(0, 100).round(2)

    # ----------------------------------------------------------------
    # Step 6b：计算 sort_score（排序分，与颜色完全分离）
    # 在 heat_score 算好后才执行，因为 sort_score 依赖 heat_score
    # ----------------------------------------------------------------
    sort_decay = float(_load_cfg().get("sort_score", {}).get("decay_factor", 0.95))
    heat_map   = dict(zip(df["sector_name"],
                          pd.to_numeric(df["heat_score"], errors="coerce").fillna(0)))
    new_sort   = update_sort_score(sector_names, heat_map, prev_sort_scores, sort_decay)
    df["sort_score"] = df["sector_name"].map(new_sort)

    # ----------------------------------------------------------------
    # Step 7：组装输出
    # ----------------------------------------------------------------
    results = []
    for _, row in df.iterrows():
        results.append({
            "trade_date":               trade_date,
            "sector_name":              row["sector_name"],
            "heat_score":               _safe_float(row["heat_score"]),
            "price_score":              _safe_float(row["_price_score"]),
            "fund_score":               _safe_float(row["_fund_score"]),
            "limit_up_score":           _safe_float(row["_limit_up_ratio_score"]),
            "rise_ratio_score":         _safe_float(row["_rise_ratio_score"]),
            "daily_capital_score":      _safe_float(row["daily_capital_score"]),
            "daily_leader_score":       _safe_float(row["daily_leader_score"]),
            "continuous_capital_score": _safe_float(row["continuous_capital_score"]),
            "continuous_leader_score":  _safe_float(row["continuous_leader_score"]),
            "sort_score":               _safe_float(row["sort_score"]),
        })

    logger.info(
        "热度计算完成 %s: %d 个板块，热度均值=%.1f，最高=%.1f（%s）",
        trade_date,
        len(results),
        df["heat_score"].mean(),
        df["heat_score"].max(),
        df.loc[df["heat_score"].idxmax(), "sector_name"],
    )
    return results


# ---------------------------------------------------------------------------
# 归一化工具
# ---------------------------------------------------------------------------

def _percentile_rank(series: pd.Series) -> pd.Series:
    """截面百分位排名：NaN 给 50 分（中性），其余转换到 [0, 100]"""
    valid_mask = series.notna()
    result = pd.Series(50.0, index=series.index)

    if valid_mask.sum() < 2:
        return result

    from scipy.stats import rankdata
    ranks = rankdata(series[valid_mask], method="average")
    pct   = (ranks - 1) / (valid_mask.sum() - 1) * 100
    result[valid_mask] = pct
    return result.round(2)


def _minmax_norm(series: pd.Series) -> pd.Series:
    """Min-Max 归一化到 [0, 100]，NaN / 全相等时给 50"""
    vmin = series.min()
    vmax = series.max()
    if pd.isna(vmin) or pd.isna(vmax) or vmin == vmax:
        return pd.Series(50.0, index=series.index)
    result = (series - vmin) / (vmax - vmin) * 100
    return result.fillna(50.0).round(2)


def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return None if np.isnan(f) else round(f, 4)
    except (TypeError, ValueError):
        return None
