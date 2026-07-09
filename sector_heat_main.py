"""
板块热度雷达 - 主入口

子命令：
  collect   采集今日（或指定日期）行业板块数据并写入 PG
  calc      对已采集数据重新计算热度指数
  draw      从数据库读取最近 N 日数据并绘制热力图
  run       collect + calc + draw 一步完成（每日定时任务用）
  backfill  补充历史数据（逐日采集指定日期范围）

用法示例：
  python sector_heat_main.py run
  python sector_heat_main.py run --date 2026-07-07
  python sector_heat_main.py backfill --start 2026-06-01 --end 2026-07-06
  python sector_heat_main.py draw --lookback 20
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ---- 日志（先于其他模块初始化）----
import yaml

_CFG_PATH = Path(__file__).parent / "config" / "config.yaml"
with open(_CFG_PATH, encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f).get("sector_heat", {})

_log_level = getattr(logging, _cfg.get("logging", {}).get("level", "INFO").upper(), logging.INFO)
_log_file  = _cfg.get("logging", {}).get("log_file", "")

_handlers: list[logging.Handler] = []

# Windows 控制台强制 utf-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_handlers.append(logging.StreamHandler(sys.stdout))
if _log_file:
    Path(_log_file).parent.mkdir(parents=True, exist_ok=True)
    _handlers.append(logging.FileHandler(_log_file, encoding="utf-8"))

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)

logger = logging.getLogger(__name__)

# ---- 业务模块 ----
from sector_heat.db import init_db, upsert_daily_raw, upsert_heat_index, \
    query_heat_matrix, query_raw_by_date, get_latest_trade_date, \
    query_prev_continuous_scores, query_prev_sort_scores, query_raw_history
from sector_heat.collector import SectorDataCollector
from sector_heat.calculator import compute_heat_for_date
from sector_heat.visualizer import draw_heatmap


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def cmd_collect(args) -> bool:
    target_date: date = args.date or date.today()
    logger.info("=== 采集 %s 数据 ===", target_date)

    cfg_col = _cfg.get("collector", {})
    collector = SectorDataCollector(
        request_interval=cfg_col.get("request_interval", 1.5),
        max_retries=cfg_col.get("max_retries", 3),
    )
    records = collector.collect(target_date)
    if not records:
        logger.error("采集失败，无数据写入")
        return False

    n = upsert_daily_raw(records)
    logger.info("已写入 %d 条原始记录", n)
    return True


def cmd_calc(args) -> bool:
    target_date: date = args.date or get_latest_trade_date() or date.today()
    logger.info("=== 计算 %s 热度指数 ===", target_date)

    raw_rows = query_raw_by_date(target_date)
    if not raw_rows:
        logger.error("数据库中无 %s 的原始数据，请先执行 collect", target_date)
        return False

    prev_scores      = query_prev_continuous_scores(target_date)
    prev_sort_scores = query_prev_sort_scores(target_date)
    history_rows     = query_raw_history(target_date, days=130)
    logger.info("前日连续分：%d 个板块  排序分：%d 个板块  历史行：%d 条",
                len(prev_scores), len(prev_sort_scores), len(history_rows))

    heat_rows = compute_heat_for_date(
        raw_rows,
        prev_continuous_scores=prev_scores,
        prev_sort_scores=prev_sort_scores,
        history_rows=history_rows,
    )
    n = upsert_heat_index(heat_rows)
    logger.info("已写入 %d 条热度记录", n)
    return True


def cmd_draw(args) -> bool:
    lookback = args.lookback or _cfg.get("visualizer", {}).get("lookback_days", 20)
    logger.info("=== 绘制热力图（最近 %d 个交易日）===", lookback)

    heat_rows = query_heat_matrix(lookback)
    if not heat_rows:
        logger.error("数据库中暂无热度数据，请先执行 collect + calc")
        return False

    path = draw_heatmap(heat_rows)
    if path and path.exists():
        logger.info("热力图已生成: %s", path)
        return True
    return False


def cmd_run(args) -> bool:
    """collect + calc + draw 一步到位"""
    ok = cmd_collect(args)
    if not ok:
        return False
    ok = cmd_calc(args)
    if not ok:
        return False
    return cmd_draw(args)


def cmd_backfill(args) -> bool:
    """批量历史回填（THS 历史指数接口，一次完成所有板块×日期）"""
    from collections import defaultdict

    start: date = args.start
    end:   date = args.end or date.today()

    cfg_col = _cfg.get("collector", {})
    collector = SectorDataCollector(
        request_interval=cfg_col.get("request_interval", 1.5),
        max_retries=cfg_col.get("max_retries", 3),
    )

    # 批量拉取所有板块历史数据（collect_range 内部已向前多拉 10 天作为缓冲）
    all_records = collector.collect_range(start, end)
    if not all_records:
        logger.error("历史回填未获取到任何数据")
        return False

    # 按日期分组
    by_date: dict[date, list[dict]] = defaultdict(list)
    for r in all_records:
        by_date[r["trade_date"]].append(r)

    # 将 start 之前的缓冲记录预填入历史缓冲区（用于 HistoryMode 滚动计算）
    history_buffer: list[dict] = [r for r in all_records if r["trade_date"] < start]
    logger.info("历史缓冲区预填 %d 条（start 前缓冲期数据）", len(history_buffer))

    success = 0
    for d in sorted(d for d in by_date if d >= start):
        records = by_date[d]

        # 写入原始数据
        upsert_daily_raw(records)

        # 取前日连续分和排序分
        prev_scores      = query_prev_continuous_scores(d)
        prev_sort_scores = query_prev_sort_scores(d)

        heat_rows = compute_heat_for_date(
            records,
            prev_continuous_scores=prev_scores,
            prev_sort_scores=prev_sort_scores,
            history_rows=history_buffer,   # 传入截至昨日的历史缓冲
        )
        upsert_heat_index(heat_rows)

        # 把今日记录追加到缓冲区，供下一交易日使用
        history_buffer.extend(records)

        success += 1
        logger.info("已写入 %s: %d 板块", d, len(records))

    logger.info("backfill 完成：%d 个交易日写入成功", success)
    return True


# ---------------------------------------------------------------------------
# CLI 解析
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    init_db()

    parser = argparse.ArgumentParser(description="板块热度雷达")
    sub = parser.add_subparsers(dest="cmd")

    # collect
    p_col = sub.add_parser("collect", help="采集指定日期行情数据")
    p_col.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD，默认今天")

    # calc
    p_calc = sub.add_parser("calc", help="重新计算热度指数")
    p_calc.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD，默认最新日期")

    # draw
    p_draw = sub.add_parser("draw", help="绘制热力图")
    p_draw.add_argument("--lookback", type=int, default=None, help="显示最近 N 个交易日")

    # run
    p_run = sub.add_parser("run", help="一步完成 collect+calc+draw")
    p_run.add_argument("--date", type=_parse_date, default=None)
    p_run.add_argument("--lookback", type=int, default=None)

    # backfill
    p_back = sub.add_parser("backfill", help="补充历史数据")
    p_back.add_argument("--start", type=_parse_date, required=True, help="开始日期 YYYY-MM-DD")
    p_back.add_argument("--end",   type=_parse_date, default=None,  help="结束日期，默认今天")

    args = parser.parse_args()

    cmd_map = {
        "collect":  cmd_collect,
        "calc":     cmd_calc,
        "draw":     cmd_draw,
        "run":      cmd_run,
        "backfill": cmd_backfill,
    }

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    fn = cmd_map.get(args.cmd)
    ok = fn(args)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
