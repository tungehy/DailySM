"""
板块热度 Skill

封装 sector_heat/ 模块的全部操作，提供统一的高层接口。
Agent 层无需直接导入 sector_heat/ 下的任何模块。

主要能力：
  collect(date)             — 当日行情采集（THS）
  collect_range(start,end)  — 历史区间批量采集
  calculate(raw, prev, ...)  — 计算热度指数
  run_full(date)            — 采集 + 计算 + 写库 + 绘图 一键运行
  draw(lookback)            — 生成热力图 PNG
  get_heat_scores(date)     — 从数据库读取热度结果
  get_latest_date()         — 最新有数据的交易日

lookback_days 和 history_buf 均从 sector_heat.yaml 读取，
显式传参时覆盖配置文件值。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


def _load_heat_cfg() -> dict:
    """加载 config.yaml 中的 sector_heat 配置块"""
    with open(_YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("sector_heat", {})


def _cfg_lookback() -> int:
    """从 config.yaml → sector_heat.visualizer.lookback_days"""
    return _load_heat_cfg().get("visualizer", {}).get("lookback_days", 20)


def _cfg_history_buf() -> int:
    """
    从 config.yaml → sector_heat.heat_engine.history.volume_percentile_period + 10
    """
    period = (
        _load_heat_cfg()
        .get("heat_engine", {})
        .get("history", {})
        .get("volume_percentile_period", 120)
    )
    return period + 10


class HeatSkill:
    """
    板块热度 Skill

    参数：
        lookback_days:  热力图回看天数（None = 读 sector_heat.yaml visualizer.lookback_days）
        history_buf:    HistoryMode 滚动计算前置天数（None = volume_percentile_period + 10）
    """

    def __init__(
        self,
        lookback_days: int | None = None,
        history_buf: int | None = None,
    ):
        self.lookback_days = lookback_days if lookback_days is not None else _cfg_lookback()
        self.history_buf   = history_buf   if history_buf   is not None else _cfg_history_buf()

    # ------------------------------------------------------------------
    # 数据采集
    # ------------------------------------------------------------------

    def collect(self, trade_date: date) -> list[dict]:
        """当日实时采集（THS summary + 涨停池）"""
        from sector_heat.collector import SectorDataCollector
        logger.info("[HeatSkill] 采集 %s 当日数据…", trade_date)
        return SectorDataCollector().collect(trade_date)

    def collect_range(self, start: date, end: date) -> list[dict]:
        """历史区间批量采集（价格+成交额，主力资金字段为 None）"""
        from sector_heat.collector import SectorDataCollector
        logger.info("[HeatSkill] 批量采集 %s ~ %s…", start, end)
        return SectorDataCollector().collect_range(start, end)

    # ------------------------------------------------------------------
    # 计算
    # ------------------------------------------------------------------

    def calculate(
        self,
        raw_rows: list[dict],
        prev_continuous_scores: dict[str, dict] | None = None,
        prev_sort_scores: dict[str, float] | None = None,
        history_rows: list[dict] | None = None,
    ) -> list[dict]:
        """
        计算热度指数（含连续得分、排序分）。
        参数含义同 calculator.compute_heat_for_date。
        """
        from sector_heat.calculator import compute_heat_for_date
        return compute_heat_for_date(
            raw_rows,
            prev_continuous_scores=prev_continuous_scores,
            prev_sort_scores=prev_sort_scores,
            history_rows=history_rows,
        )

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------

    def store_raw(self, rows: list[dict]) -> int:
        """写入 sector_daily_raw"""
        from sector_heat.db import upsert_daily_raw
        return upsert_daily_raw(rows)

    def store_heat(self, rows: list[dict]) -> int:
        """写入 sector_heat_index"""
        from sector_heat.db import upsert_heat_index
        return upsert_heat_index(rows)

    def get_heat_scores(self, lookback_days: int | None = None) -> list[dict]:
        """读取热度矩阵（最近 N 个交易日）"""
        from sector_heat.db import query_heat_matrix
        return query_heat_matrix(lookback_days or self.lookback_days)

    def get_heat_scores_by_date(self, trade_date: date) -> list[dict]:
        """读取指定日期的热度记录"""
        rows = self.get_heat_scores(lookback_days=30)
        return [r for r in rows if r["trade_date"] == trade_date]

    def get_raw_by_date(self, trade_date: date) -> list[dict]:
        """读取指定日期原始数据"""
        from sector_heat.db import query_raw_by_date
        return query_raw_by_date(trade_date)

    def get_raw_history(self, before_date: date, days: int | None = None) -> list[dict]:
        """读取历史原始数据（用于 HistoryModeStrategy）"""
        from sector_heat.db import query_raw_history
        return query_raw_history(before_date, days or self.history_buf)

    def get_prev_continuous_scores(self, trade_date: date) -> dict[str, dict]:
        from sector_heat.db import query_prev_continuous_scores
        return query_prev_continuous_scores(trade_date)

    def get_prev_sort_scores(self, trade_date: date) -> dict[str, float]:
        from sector_heat.db import query_prev_sort_scores
        return query_prev_sort_scores(trade_date)

    def get_latest_date(self) -> date | None:
        from sector_heat.db import get_latest_trade_date
        return get_latest_trade_date()

    def init_db(self) -> None:
        from sector_heat.db import init_db
        init_db()

    # ------------------------------------------------------------------
    # 可视化
    # ------------------------------------------------------------------

    def draw(self, output_dir: Path | None = None) -> Path:
        """生成热力图 PNG（使用数据库中最近 lookback_days 数据）"""
        from sector_heat.db         import query_heat_matrix
        from sector_heat.visualizer import draw_heatmap
        rows = query_heat_matrix(self.lookback_days)
        return draw_heatmap(rows, output_dir=output_dir)

    def export_json(self, json_path: Path) -> None:
        """导出热力图 JSON（用于前端展示）"""
        from sector_heat.db         import query_heat_matrix
        from sector_heat.visualizer import export_json
        rows = query_heat_matrix(self.lookback_days)
        export_json(rows, json_path)

    # ------------------------------------------------------------------
    # 一键全流程
    # ------------------------------------------------------------------

    def run_full(self, trade_date: date) -> bool:
        """
        完整日常流程：采集 → 写库 → 计算 → 写库 → 绘图。

        Returns:
            True = 成功，False = 失败
        """
        try:
            self.init_db()

            # 1. 采集
            raw_rows = self.collect(trade_date)
            if not raw_rows:
                logger.warning("[HeatSkill] 采集结果为空，跳过 %s", trade_date)
                return False

            # 2. 写原始数据
            self.store_raw(raw_rows)

            # 3. 取前置数据
            prev_scores      = self.get_prev_continuous_scores(trade_date)
            prev_sort        = self.get_prev_sort_scores(trade_date)
            history_rows     = self.get_raw_history(trade_date)

            # 4. 计算热度
            heat_rows = self.calculate(
                raw_rows,
                prev_continuous_scores=prev_scores,
                prev_sort_scores=prev_sort,
                history_rows=history_rows,
            )

            # 5. 写热度指数
            self.store_heat(heat_rows)

            # 6. 绘图
            self.draw()

            logger.info("[HeatSkill] %s 全流程完成，%d 个板块", trade_date, len(heat_rows))
            return True

        except Exception as e:
            logger.error("[HeatSkill] run_full 失败: %s", e, exc_info=True)
            return False
