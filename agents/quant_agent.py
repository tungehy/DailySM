"""
量化分析 Agent

基于四种纯公式量化模型对行业板块进行评分，
输出综合量化得分并给出板块强弱排名。

四种模型（均使用经典参数值）：
────────────────────────────────────────────────────────────────────
Model 1  趋势动量模型 (Trend Momentum)
  原理：多周期累计涨幅加权，短期权重更高
  参数：windows=[5, 10, 20]，weights=[0.5, 0.3, 0.2]
  公式：composite = Σ(weight_i × cum_return_i)
        score    = 截面百分位排名 → 0~100

Model 2  成交额放量模型 (Volume Expansion)
  原理：评估今日成交额相对均值的放量程度，上涨时放量额外奖励
  参数：ma_window=20，direction_bonus=0.3
  公式：vol_ratio  = today_vol / MA20(vol)
        dir_factor = 1.0 + 0.3 × (price_change > 0)
        raw_score  = vol_ratio × dir_factor (clip 0~5)
        score      = 截面百分位排名 → 0~100

Model 3  相对强弱模型 (Relative Strength vs Benchmark)
  原理：板块超额收益相对沪深300，区分短中期
  参数：windows=[5, 20]，weights=[0.6, 0.4]，benchmark=沪深300
  公式：RS_i = cum_return_sector_i - cum_return_bench
        composite = 0.6×RS_5 + 0.4×RS_20
        score     = 截面百分位排名 → 0~100

Model 4  趋势评分模型 (MA Alignment Trend Score)
  原理：多均线多头排列程度 + 价格偏离MA20
  参数：ma_windows=[5,10,20,60]，align_weights=[0.1,0.2,0.3,0.4]
        dist_weight=0.3，align_weight=0.7
  公式：重建价格指数 P(t) = cumprod(1 + pct/100) × 100
        alignment = Σ(w_i × bool(MA_i > MA_{i+1}))  范围[0,1]
        dist_20   = (P[-1] - MA20) / MA20（偏离MA20的百分比）
        raw_score = 0.7 × alignment + 0.3 × max(dist_20_norm, 0)
        score     = 截面百分位排名 → 0~100

综合得分：
  quant_score = 0.30×momentum + 0.25×vol_expansion + 0.25×rs + 0.20×trend
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from agents.base import AgentResult, BaseAgent
from skills.quant_db import (
    init_quant_table,
    query_quant_latest,
    query_sector_price_volume_including,
    upsert_quant_scores,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模型参数（经典值，无需外部配置）
# ---------------------------------------------------------------------------

# Model 1: 趋势动量
_MOM_WINDOWS  = [5, 10, 20]       # 动量计算窗口（交易日）
_MOM_WEIGHTS  = [0.5, 0.3, 0.2]   # 短中长期权重（短期更高）

# Model 2: 成交额放量
_VOL_MA_WIN      = 20    # 成交额基准均线周期
_VOL_DIR_BONUS   = 0.3   # 放量上涨的额外加成系数
_VOL_CAP         = 5.0   # 放量比值上限

# Model 3: 相对强弱
_RS_WINDOWS  = [5, 20]        # 超额收益计算窗口
_RS_WEIGHTS  = [0.6, 0.4]     # 短中期权重
_BENCH_CODE  = "sh000300"     # 基准指数：沪深300

# Model 4: 趋势评分
_MA_WINDOWS       = [5, 10, 20, 60]       # 均线周期
_MA_ALIGN_WEIGHTS = [0.1, 0.2, 0.3, 0.4]  # 多头排列权重（MA20>MA60权重最高）
_TREND_ALIGN_W    = 0.7                    # 排列得分占比
_TREND_DIST_W     = 0.3                    # 偏离MA20占比
_DIST_CLIP        = 0.15                   # 偏离MA20最大绝对值（15%裁剪）

# 综合得分权重
_COMPOSITE_WEIGHTS = {
    "momentum":    0.30,
    "vol":         0.25,
    "rs":          0.25,
    "trend":       0.20,
}

# 所需历史交易日天数（MA60 需要约 65 天 + 缓冲）
_HISTORY_DAYS = 80


# ---------------------------------------------------------------------------
# 截面归一化工具
# ---------------------------------------------------------------------------

def _percentile_rank(s: pd.Series, default: float = 50.0) -> pd.Series:
    """截面百分位排名 → [0, 100]，NaN 赋默认值"""
    valid = s.notna()
    result = pd.Series(default, index=s.index)
    n = valid.sum()
    if n < 2:
        return result
    ranks = rankdata(s[valid], method="average")
    result[valid] = (ranks - 1) / (n - 1) * 100
    return result.round(2)


# ---------------------------------------------------------------------------
# 量化 Agent
# ---------------------------------------------------------------------------

class QuantAgent(BaseAgent):
    """
    量化分析 Agent

    参数：
        top_n:         输出推荐板块数量
        save_to_db:    是否将量化得分写入 sector_quant_index 表
    """

    name        = "quant"
    description = "量化分析（趋势动量 + 成交额放量 + 相对强弱 + 趋势评分，纯公式无ML）"

    def __init__(self, top_n: int = 10, save_to_db: bool = True):
        self.top_n     = top_n
        self.save_to_db= save_to_db

    def is_available(self) -> bool:
        """检查是否有足够的历史数据"""
        try:
            from sector_heat.db import get_latest_trade_date
            latest = get_latest_trade_date()
            if latest is None:
                return False
            # 粗略检查：有数据即可（具体够不够在 analyze 中处理）
            return True
        except Exception:
            return False

    def analyze(self, trade_date: date) -> AgentResult:
        try:
            return self._run(trade_date)
        except Exception as e:
            logger.error("[QuantAgent] 分析失败: %s", e, exc_info=True)
            return self._empty_result(trade_date, str(e))

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _run(self, trade_date: date) -> AgentResult:
        # 1. 检查 DB 是否已有当日量化得分（避免重复计算）
        try:
            init_quant_table()
            existing = query_quant_latest(trade_date)
            if existing:
                logger.info("[QuantAgent] 从缓存读取 %s 量化得分（%d 板块）", trade_date, len(existing))
                return self._format_result(trade_date, existing)
        except Exception as e:
            logger.debug("[QuantAgent] 缓存读取失败: %s", e)

        # 2. 获取历史价格+成交额数据（含今日）
        logger.info("[QuantAgent] 计算 %s 量化得分（历史 %d 天）…", trade_date, _HISTORY_DAYS)
        history = query_sector_price_volume_including(trade_date, days=_HISTORY_DAYS)
        if not history:
            return self._empty_result(trade_date, "无历史原始数据，请先运行热度回填")

        # 3. 构建 pivot 表
        df = pd.DataFrame(history)
        df["price_change_pct"] = pd.to_numeric(df["price_change_pct"], errors="coerce").fillna(0)
        df["turnover_amount"]  = pd.to_numeric(df["turnover_amount"],  errors="coerce").fillna(0)

        pct_pivot = df.pivot_table(
            index="trade_date", columns="sector_name",
            values="price_change_pct", aggfunc="last",
        ).astype(float)
        vol_pivot = df.pivot_table(
            index="trade_date", columns="sector_name",
            values="turnover_amount", aggfunc="last",
        ).astype(float)

        # 4. 获取基准指数（沪深300）
        bench_pct = self._fetch_benchmark(trade_date)

        # 确保今日数据在最后一行
        today_sectors = df[df["trade_date"] == trade_date]["sector_name"].tolist()
        if not today_sectors:
            return self._empty_result(trade_date, f"无 {trade_date} 当日数据")

        # 5. 运行四个模型
        logger.info("[QuantAgent] 运行四模型计算（板块数=%d，历史日=%d）…",
                    len(pct_pivot.columns), len(pct_pivot))
        m1 = self._model_momentum(pct_pivot)
        m2 = self._model_volume_expansion(pct_pivot, vol_pivot)
        m3 = self._model_relative_strength(pct_pivot, bench_pct)
        m4 = self._model_trend_score(pct_pivot)

        # 6. 综合得分
        quant_score = (
            m1 * _COMPOSITE_WEIGHTS["momentum"] +
            m2 * _COMPOSITE_WEIGHTS["vol"]      +
            m3 * _COMPOSITE_WEIGHTS["rs"]       +
            m4 * _COMPOSITE_WEIGHTS["trend"]
        ).round(2)

        # 7. 构造详细中间结果（含各模型原始中间量）
        rows = self._build_db_rows(
            trade_date, pct_pivot, vol_pivot, bench_pct,
            m1, m2, m3, m4, quant_score,
        )

        # 8. 写库
        if self.save_to_db:
            try:
                upsert_quant_scores(rows)
            except Exception as e:
                logger.warning("[QuantAgent] 写库失败（不影响结果）: %s", e)

        # 9. 格式化 AgentResult
        return self._format_result(trade_date, rows)

    # ------------------------------------------------------------------
    # Model 1: 趋势动量
    # ------------------------------------------------------------------

    def _model_momentum(self, pct_df: pd.DataFrame) -> pd.Series:
        """
        多周期累计涨幅加权动量。
        取每个窗口内所有日期的涨跌幅之和（近似累计收益率）。
        截面百分位排名 → 0~100。
        """
        components = pd.DataFrame(index=pct_df.columns)
        for w, weight in zip(_MOM_WINDOWS, _MOM_WEIGHTS):
            if len(pct_df) < w:
                components[f"m{w}"] = 0.0
                continue
            cum_ret = pct_df.tail(w).sum()
            components[f"m{w}"] = cum_ret * weight

        raw = components.sum(axis=1)
        score = _percentile_rank(raw)
        logger.debug("[QuantAgent] 动量模型：均值=%.1f  范围=[%.1f, %.1f]",
                     score.mean(), score.min(), score.max())
        return score

    # ------------------------------------------------------------------
    # Model 2: 成交额放量
    # ------------------------------------------------------------------

    def _model_volume_expansion(
        self, pct_df: pd.DataFrame, vol_df: pd.DataFrame,
    ) -> pd.Series:
        """
        今日成交额 / MA20，乘以方向加成系数。
        上涨放量比下跌放量信号更强。
        截面百分位排名 → 0~100。
        """
        if len(vol_df) < _VOL_MA_WIN + 1:
            logger.warning("[QuantAgent] 放量模型：历史数据不足 %d 天，返回默认50", _VOL_MA_WIN + 1)
            return pd.Series(50.0, index=vol_df.columns)

        # 今日值（最后一行）
        vol_today  = vol_df.iloc[-1]
        # MA20 不含今日（用 -(_VOL_MA_WIN+1):-1 切片）
        vol_ma20   = vol_df.iloc[-(_VOL_MA_WIN + 1):-1].mean()
        vol_ma20   = vol_ma20.replace(0, np.nan)
        vol_ratio  = (vol_today / vol_ma20).clip(0, _VOL_CAP).fillna(1.0)

        # 方向系数：今日涨为正
        pct_today     = pct_df.iloc[-1]
        dir_factor    = 1.0 + _VOL_DIR_BONUS * (pct_today > 0).astype(float)

        raw   = (vol_ratio * dir_factor).round(4)
        score = _percentile_rank(raw)
        logger.debug("[QuantAgent] 放量模型：vol_ratio均=%.2f  score均=%.1f",
                     vol_ratio.mean(), score.mean())
        return score

    # ------------------------------------------------------------------
    # Model 3: 相对强弱
    # ------------------------------------------------------------------

    def _model_relative_strength(
        self, pct_df: pd.DataFrame, bench_pct: pd.Series,
    ) -> pd.Series:
        """
        板块多周期累计收益 - 沪深300累计收益（超额收益）。
        短中期加权，截面百分位排名 → 0~100。
        """
        components = pd.DataFrame(index=pct_df.columns)
        for w, weight in zip(_RS_WINDOWS, _RS_WEIGHTS):
            # 板块累计
            sec_cum = pct_df.tail(w).sum() if len(pct_df) >= w else pct_df.sum()
            # 基准累计
            bench_cum = (
                float(bench_pct.tail(w).sum())
                if bench_pct is not None and len(bench_pct) >= w
                else 0.0
            )
            components[f"rs{w}"] = (sec_cum - bench_cum) * weight

        raw   = components.sum(axis=1)
        score = _percentile_rank(raw)
        logger.debug("[QuantAgent] 相对强弱：raw均=%.2f  score均=%.1f",
                     raw.mean(), score.mean())
        return score

    # ------------------------------------------------------------------
    # Model 4: 趋势评分（MA 多头排列）
    # ------------------------------------------------------------------

    def _model_trend_score(self, pct_df: pd.DataFrame) -> pd.Series:
        """
        多均线多头排列程度 + 价格偏离MA20。
        重建价格指数 P(t) = cumprod(1 + pct/100) × 100。
        截面百分位排名 → 0~100。
        """
        # 重建价格指数（逐列）
        price_index = (1 + pct_df / 100.0).cumprod() * 100.0
        raw_scores: dict[str, float] = {}

        for sector in price_index.columns:
            P = price_index[sector].dropna()
            if len(P) < 10:
                raw_scores[sector] = 0.5
                continue

            p_last = float(P.iloc[-1])
            # 计算各均线（不足时用实际数据）
            ma_vals: dict[int, float] = {}
            for w in _MA_WINDOWS:
                ma_vals[w] = float(P.tail(w).mean()) if len(P) >= w else p_last

            # 多头排列得分
            # 检查顺序：price > MA5 > MA10 > MA20 > MA60
            alignment_checks = [
                (p_last     > ma_vals[5],  _MA_ALIGN_WEIGHTS[0]),  # 价格 > MA5
                (ma_vals[5] > ma_vals[10], _MA_ALIGN_WEIGHTS[1]),  # MA5  > MA10
                (ma_vals[10]> ma_vals[20], _MA_ALIGN_WEIGHTS[2]),  # MA10 > MA20
                (ma_vals[20]> ma_vals[60], _MA_ALIGN_WEIGHTS[3]),  # MA20 > MA60 ★
            ]
            alignment = sum(w for cond, w in alignment_checks if cond)  # [0, 1]

            # 偏离 MA20（正值=在均线上方，取 max 避免空头打分）
            dist_20 = (p_last - ma_vals[20]) / ma_vals[20] if ma_vals[20] else 0.0
            dist_norm = np.clip(dist_20 / _DIST_CLIP, -1.0, 1.0)   # [-1, 1]
            dist_positive = max(dist_norm, 0.0)                      # [0, 1]

            raw_scores[sector] = (
                _TREND_ALIGN_W * alignment +
                _TREND_DIST_W  * dist_positive
            )

        raw   = pd.Series(raw_scores)
        score = _percentile_rank(raw)
        logger.debug("[QuantAgent] 趋势评分：raw均=%.3f  score均=%.1f",
                     raw.mean(), score.mean())
        return score

    # ------------------------------------------------------------------
    # 基准指数
    # ------------------------------------------------------------------

    def _fetch_benchmark(self, trade_date: date) -> pd.Series | None:
        """获取沪深300历史日涨跌幅（最近 _HISTORY_DAYS 天）"""
        try:
            import akshare as ak
            from datetime import timedelta
            start = trade_date - timedelta(days=_HISTORY_DAYS * 2)  # 日历天加倍以覆盖交易日
            df = ak.stock_zh_index_daily(symbol=_BENCH_CODE)
            if df is None or df.empty:
                return None
            df = df.sort_index()
            # 转为日涨跌幅 %
            df["pct"] = df["close"].pct_change() * 100
            # 统一索引为字符串日期，确保比较兼容
            df.index = df.index.astype(str).str[:10]
            df_filtered = df[df.index <= str(trade_date)].tail(_HISTORY_DAYS)
            result = df_filtered["pct"].fillna(0)
            logger.debug("[QuantAgent] 基准数据: %d 天", len(result))
            return result
        except Exception as e:
            logger.warning("[QuantAgent] 无法获取基准数据，RS 模型将用 0 作基准: %s", e)
            return None

    # ------------------------------------------------------------------
    # 构建 DB 行
    # ------------------------------------------------------------------

    def _build_db_rows(
        self,
        trade_date: date,
        pct_df: pd.DataFrame,
        vol_df: pd.DataFrame,
        bench_pct: pd.Series | None,
        m1: pd.Series, m2: pd.Series, m3: pd.Series, m4: pd.Series,
        quant_score: pd.Series,
    ) -> list[dict]:
        """构建 sector_quant_index 写库所需的字典列表"""
        rows: list[dict] = []

        # 各窗口中间量（供写库记录）
        mom_5  = pct_df.tail(5).sum()  if len(pct_df) >= 5  else pd.Series(dtype=float)
        mom_10 = pct_df.tail(10).sum() if len(pct_df) >= 10 else pd.Series(dtype=float)
        mom_20 = pct_df.tail(20).sum() if len(pct_df) >= 20 else pd.Series(dtype=float)

        vol_today = vol_df.iloc[-1]
        vol_ma20  = (
            vol_df.iloc[-(_VOL_MA_WIN + 1):-1].mean().replace(0, np.nan)
            if len(vol_df) > _VOL_MA_WIN else pd.Series(np.nan, index=vol_df.columns)
        )
        vol_ratio_20 = (vol_today / vol_ma20).clip(0, _VOL_CAP).fillna(1.0)
        pct_today    = pct_df.iloc[-1]

        # 超额收益中间量
        bench_5d  = float(bench_pct.tail(5).sum())  if bench_pct is not None and len(bench_pct)>=5  else 0.0
        bench_20d = float(bench_pct.tail(20).sum()) if bench_pct is not None and len(bench_pct)>=20 else 0.0
        sec_5d  = pct_df.tail(5).sum()
        sec_20d = pct_df.tail(20).sum()

        # MA 排列（用于写库）
        price_index = (1 + pct_df / 100.0).cumprod() * 100.0
        for sector in quant_score.index:
            P = price_index[sector].dropna() if sector in price_index.columns else pd.Series(dtype=float)
            p_last = float(P.iloc[-1]) if len(P) > 0 else np.nan
            ma20   = float(P.tail(20).mean()) if len(P) >= 20 else p_last
            # MA alignment 取 0~1 原始值
            p5, p10, p20, p60 = [
                float(P.tail(w).mean()) if len(P) >= w else p_last
                for w in [5, 10, 20, 60]
            ]
            alignment = (
                _MA_ALIGN_WEIGHTS[0] * float(p_last > p5 if not np.isnan(p_last) else False) +
                _MA_ALIGN_WEIGHTS[1] * float(p5 > p10) +
                _MA_ALIGN_WEIGHTS[2] * float(p10 > p20) +
                _MA_ALIGN_WEIGHTS[3] * float(p20 > p60)
            )
            dist_ma20 = ((p_last - ma20) / ma20 * 100) if (ma20 and not np.isnan(p_last)) else 0.0

            def _safe(s: pd.Series, col: str, default=None):
                v = s.get(col, default) if isinstance(s, pd.Series) else default
                return round(float(v), 4) if v is not None and not np.isnan(float(v if v is not None else 0)) else None

            rows.append({
                "trade_date":          trade_date,
                "sector_name":         sector,
                "momentum_5d":         _safe(mom_5,  sector),
                "momentum_10d":        _safe(mom_10, sector),
                "momentum_20d":        _safe(mom_20, sector),
                "momentum_score":      round(float(m1.get(sector, 50)), 2),
                "vol_ratio_20":        round(float(vol_ratio_20.get(sector, 1.0)), 4),
                "vol_direction_pct":   round(float(pct_today.get(sector, 0)), 2),
                "vol_expansion_score": round(float(m2.get(sector, 50)), 2),
                "rs_vs_bench_5d":      round(float(sec_5d.get(sector, 0))  - bench_5d,  2),
                "rs_vs_bench_20d":     round(float(sec_20d.get(sector, 0)) - bench_20d, 2),
                "rs_score":            round(float(m3.get(sector, 50)), 2),
                "ma_alignment":        round(alignment, 4),
                "dist_from_ma20":      round(dist_ma20, 2),
                "trend_score":         round(float(m4.get(sector, 50)), 2),
                "quant_score":         round(float(quant_score.get(sector, 50)), 2),
            })
        return rows

    # ------------------------------------------------------------------
    # 格式化 AgentResult
    # ------------------------------------------------------------------

    def _format_result(
        self,
        trade_date: date,
        rows: list[dict],
    ) -> AgentResult:
        """将量化得分转换为统一 AgentResult 格式"""
        # 转换 Decimal → float（DB 返回 Decimal 类型）
        def _f(v, default=50.0):
            if v is None:
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        # 排序：quant_score 高到低
        sorted_rows = sorted(rows, key=lambda r: _f(r.get("quant_score")), reverse=True)

        top_sectors: list[dict] = []
        for r in sorted_rows[: self.top_n]:
            qs = _f(r.get("quant_score"))
            top_sectors.append({
                "sector":    r["sector_name"],
                "score":     round(qs, 1),
                "direction": "bullish" if qs >= 65 else ("bearish" if qs <= 35 else "neutral"),
                "reason":    self._build_reason(r),
            })

        # 补充末尾最弱板块（bearish）
        for r in sorted_rows[-5:]:
            qs = _f(r.get("quant_score"))
            if qs <= 30:
                top_sectors.append({
                    "sector":    r["sector_name"],
                    "score":     round(qs, 1),
                    "direction": "bearish",
                    "reason":    f"量化综合得分{qs:.0f}，四模型均偏弱",
                })

        # 摘要文本
        hot_names = "、".join(s["sector"] for s in top_sectors[:5] if s["direction"] == "bullish")
        cold_names = "、".join(s["sector"] for s in top_sectors if s["direction"] == "bearish")[:3]
        summary = (
            f"## 量化分析报告（{trade_date}）\n\n"
            f"**模型**：趋势动量(30%) + 成交额放量(25%) + 相对强弱(25%) + 趋势评分(20%)\n\n"
            f"**量化强势板块（前5）**：{hot_names or '无'}\n\n"
            f"**量化弱势板块**：{cold_names or '无'}\n\n"
            "### 评分说明\n"
            "- 趋势动量：5/10/20日累计涨幅加权（0.5/0.3/0.2）\n"
            "- 成交额放量：今日/MA20 × 方向系数（上涨+30%加成）\n"
            "- 相对强弱：vs 沪深300超额收益（5/20日权重0.6/0.4）\n"
            "- 趋势评分：MA多头排列（5/10/20/60日）+ 偏离MA20\n"
        )

        # 覆盖度评估
        has_vol = sum(1 for r in rows if r.get("vol_ratio_20") and _f(r.get("vol_ratio_20")) > 0)
        confidence = min(1.0, 0.4 + 0.6 * (has_vol / max(len(rows), 1)))

        result = AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = summary,
            top_sectors = top_sectors,
            confidence  = round(confidence, 2),
            mode        = "quant_4models",
            raw_data    = {
                "total_sectors": len(rows),
                "top10_quant": [
                    {
                        "sector":        r["sector_name"],
                        "quant_score":   _f(r.get("quant_score")),
                        "momentum":      _f(r.get("momentum_score")),
                        "vol_expansion": _f(r.get("vol_expansion_score")),
                        "rs":            _f(r.get("rs_score")),
                        "trend":         _f(r.get("trend_score")),
                    }
                    for r in sorted_rows[:10]
                ],
            },
        )

        # 保存到 Analysis Repository
        try:
            from sector_heat.db import get_engine
            from skills.analysis_repo import init_analysis_table
            engine = get_engine()
            init_analysis_table(engine)
            self.save_result(result, engine, expire_hours=20)
        except Exception as e:
            logger.warning("[QuantAgent] 保存到 analysis_results 失败: %s", e)

        return result

    @staticmethod
    def _build_reason(r: dict) -> str:
        """根据四模型子分构建一句话说明"""
        def _f(v):
            try:
                return float(v) if v is not None else 50.0
            except (TypeError, ValueError):
                return 50.0

        parts = []
        ms = _f(r.get("momentum_score"))
        if ms >= 70:
            parts.append(f"动量强({ms:.0f})")
        vs = _f(r.get("vol_expansion_score"))
        if vs >= 70:
            parts.append(f"放量({vs:.0f})")
        rs = _f(r.get("rs_score"))
        if rs >= 70:
            parts.append(f"超额收益强({rs:.0f})")
        ts = _f(r.get("trend_score"))
        if ts >= 70:
            parts.append(f"趋势多头({ts:.0f})")
        if not parts:
            qs = _f(r.get("quant_score"))
            parts.append(f"量化综合分{qs:.0f}")
        return "，".join(parts)
