"""
行业板块 AI 研究平台 - 调度守护进程

使用 APScheduler 实现定时触发和事件驱动调度：

  NBS 数据任务（nbs_tasks）
    - cpi_ppi          每月9日  10:00
    - purchase_manager 每月1日  09:00
    - industrial_yoy   每月15日 10:00
    - industrial_profit 每月27日 10:00
    完成后发布 "nbs_updated" 事件 → 触发 MacroAgent 重新分析

  Agent 定时分析（agents）
    - heat             工作日  15:30
    - news             工作日  08:00
    - quant            工作日  16:00
    - macro            事件驱动（不走 cron，由 NBS 任务完成后触发）

  Video Task Group（video_groups）
    - group_a          工作日  17:00
    - group_b          每周日  19:00
    - group_c          每月28日 20:00

  run-all 汇总（run_all）
    - 工作日 18:00 → freshness check + 生成决策报告

使用方式：
  python scheduler.py               # 启动守护进程（Ctrl+C 退出）
  python scheduler.py --list        # 列出任务
  python scheduler.py --dry-run     # 验证配置，不实际启动
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT   = Path(__file__).parent
_SCHEDULES_PATH = _PROJECT_ROOT / "config" / "schedules.yaml"


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _load_schedules() -> dict:
    with open(_SCHEDULES_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_cron(cron_str: str) -> dict:
    """
    将 5 位 cron 字符串解析为 APScheduler CronTrigger kwargs。
    支持标准 5 位：分 时 日 月 星期
    """
    parts = cron_str.strip().split()
    if len(parts) != 5:
        raise ValueError(f"无效 cron 表达式（需 5 位）: {cron_str!r}")
    minute, hour, day, month, day_of_week = parts
    kwargs: dict[str, Any] = {}
    if minute    != "*": kwargs["minute"]       = minute
    if hour      != "*": kwargs["hour"]         = hour
    if day       != "*": kwargs["day"]          = day
    if month     != "*": kwargs["month"]        = month
    if day_of_week != "*": kwargs["day_of_week"] = day_of_week
    return kwargs


# ---------------------------------------------------------------------------
# 任务函数
# ---------------------------------------------------------------------------

def _notify_error(source: str, message: str) -> None:
    """通过通知中心推送调度器错误告警（非阻塞）"""
    try:
        from skills.notification import get_notification_center
        get_notification_center().notify_error(f"scheduler:{source}", message)
    except Exception as e:
        logger.debug("[Scheduler] 通知推送失败: %s", e)


def _run_nbs_task(task_name: str, schedules_cfg: dict) -> None:
    """拉取 NBS 数据，完成后若 trigger_macro=True 则立即触发 MacroAgent"""
    logger.info("[Scheduler] NBS 任务开始: %s", task_name)
    try:
        from sector_heat.db import get_engine
        from skills.nbs_crawler import NBSCrawler

        engine   = get_engine()
        crawler  = NBSCrawler()
        rows, dv = crawler.fetch_task(task_name, schedules_cfg, engine=engine, store=True)
        logger.info("[Scheduler] NBS[%s] 完成，%d 条，data_version=%s", task_name, len(rows), dv[:19] if dv else "N/A")

        task_cfg = schedules_cfg.get("nbs_tasks", {}).get(task_name, {})
        if task_cfg.get("trigger_macro") and dv:
            _run_macro_agent(force=True)

    except Exception as e:
        logger.error("[Scheduler] NBS[%s] 失败: %s", task_name, e, exc_info=True)
        _notify_error(f"nbs_{task_name}", str(e))


def _run_macro_agent(force: bool = False) -> None:
    """触发 MacroAgent 分析（事件驱动）"""
    logger.info("[Scheduler] 触发 MacroAgent（force=%s）", force)
    try:
        from agents.macro_agent import MacroAgent
        agent  = MacroAgent()
        result = agent.analyze(date.today(), force=force)
        logger.info(
            "[Scheduler] MacroAgent 完成（置信度=%.0f%%，板块=%d个）",
            result.confidence * 100, len(result.top_sectors),
        )
        _notify_agent_complete(result)
    except Exception as e:
        logger.error("[Scheduler] MacroAgent 失败: %s", e, exc_info=True)
        _notify_error("MacroAgent", str(e))


def _run_heat_agent() -> None:
    logger.info("[Scheduler] 触发 HeatAgent")
    try:
        from agents.heat_agent import HeatAgent
        result = HeatAgent().analyze(date.today())
        logger.info("[Scheduler] HeatAgent 完成（置信度=%.0f%%）", result.confidence * 100)
        _notify_agent_complete(result)
    except Exception as e:
        logger.error("[Scheduler] HeatAgent 失败: %s", e, exc_info=True)
        _notify_error("HeatAgent", str(e))


def _run_news_agent() -> None:
    logger.info("[Scheduler] 触发 NewsAgent")
    try:
        from agents.news_agent import NewsAgent
        result = NewsAgent().analyze(date.today())
        logger.info("[Scheduler] NewsAgent 完成（置信度=%.0f%%）", result.confidence * 100)
        _notify_agent_complete(result)
    except Exception as e:
        logger.error("[Scheduler] NewsAgent 失败: %s", e, exc_info=True)
        _notify_error("NewsAgent", str(e))


def _run_quant_agent() -> None:
    logger.info("[Scheduler] 触发 QuantAgent")
    try:
        from agents.quant_agent import QuantAgent
        result = QuantAgent().analyze(date.today())
        logger.info("[Scheduler] QuantAgent 完成（置信度=%.0f%%）", result.confidence * 100)
        _notify_agent_complete(result)
    except Exception as e:
        logger.error("[Scheduler] QuantAgent 失败: %s", e, exc_info=True)
        _notify_error("QuantAgent", str(e))


def _run_video_group(group: str) -> None:
    logger.info("[Scheduler] 触发 VideoAgent group=%s", group)
    try:
        from agents.video_agent import VideoAgent
        result = VideoAgent(group=group, run_pipeline=False).analyze(date.today(), force=True)
        logger.info("[Scheduler] VideoAgent[%s] 完成（置信度=%.0f%%）", group, result.confidence * 100)
        _notify_agent_complete(result)
    except Exception as e:
        logger.error("[Scheduler] VideoAgent[%s] 失败: %s", group, e, exc_info=True)
        _notify_error(f"VideoAgent[{group}]", str(e))


def _notify_agent_complete(result) -> None:
    """通过通知中心推送 Agent 完成通知（on_agent_complete 默认关闭）"""
    try:
        from skills.notification import get_notification_center
        get_notification_center().notify_agent_complete(result)
    except Exception as e:
        logger.debug("[Scheduler] Agent完成通知推送失败: %s", e)


def _run_all() -> None:
    """工作日 run-all：freshness check + 生成决策报告"""
    logger.info("[Scheduler] 触发 run-all")
    try:
        from agents.base           import BaseAgent
        from agents.heat_agent     import HeatAgent
        from agents.macro_agent    import MacroAgent
        from agents.news_agent     import NewsAgent
        from agents.video_agent    import VideoAgent
        from agents.quant_agent    import QuantAgent
        from agents.decision_agent import DecisionAgent
        from sector_heat.db        import get_engine
        from skills.analysis_repo  import init_analysis_table, load_latest_as_result

        engine        = get_engine()
        init_analysis_table(engine)
        schedules_cfg = _load_schedules()
        agents_sched  = schedules_cfg.get("agents", {})

        all_agents = {
            "heat":  HeatAgent(),
            "macro": MacroAgent(),
            "news":  NewsAgent(),
            "video": VideoAgent(group="group_a"),
            "quant": QuantAgent(),
        }

        today   = date.today()
        results = []

        for name, agent in all_agents.items():
            expire_h      = int(agents_sched.get(name, {}).get("expire_hours", 24))
            analysis_type = "video_group_a" if name == "video" else ""

            if BaseAgent.check_fresh(engine, name, analysis_type, expire_h):
                cached = load_latest_as_result(engine, name, analysis_type)
                if cached:
                    logger.info("[Scheduler/run-all] [%s] 使用缓存结果", name)
                    results.append(cached)
                    continue

            if not agent.is_available():
                logger.warning("[Scheduler/run-all] [%s] 不可用，跳过", name)
                continue

            try:
                r = agent.analyze(today)
                results.append(r)
                logger.info("[Scheduler/run-all] [%s] 完成（置信度=%.0f%%）", name, r.confidence * 100)
            except Exception as e:
                logger.error("[Scheduler/run-all] [%s] 失败: %s", name, e)

        if results:
            decision = DecisionAgent(save_report=True)
            final    = decision.aggregate(results, trade_date=today)
            logger.info(
                "[Scheduler/run-all] 决策报告完成，Top3: %s",
                "、".join(s["sector"] for s in final.top_sectors[:3]),
            )
            # 决策日报通知由 DecisionAgent.aggregate() 内部统一触发
    except Exception as e:
        logger.error("[Scheduler] run-all 失败: %s", e, exc_info=True)
        _notify_error("scheduler:run_all", str(e))


# ---------------------------------------------------------------------------
# AgentScheduler
# ---------------------------------------------------------------------------

class AgentScheduler:
    """
    APScheduler 包装器，从 config/schedules.yaml 读取配置并注册所有任务。
    """

    def __init__(self, config_path: str | Path = _SCHEDULES_PATH):
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError as e:
            raise ImportError(
                "APScheduler 未安装，请运行: pip install apscheduler>=3.10.0"
            ) from e

        self.scheduler     = BlockingScheduler(timezone="Asia/Shanghai")
        self.config_path   = Path(config_path)
        self.schedules_cfg = _load_schedules()
        self._setup_all()

    # ------------------------------------------------------------------
    # 注册所有任务
    # ------------------------------------------------------------------

    def _setup_all(self) -> None:
        self._setup_nbs_tasks()
        self._setup_agent_tasks()
        self._setup_video_groups()
        self._setup_run_all()

    def _setup_nbs_tasks(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        for task_name, cfg in self.schedules_cfg.get("nbs_tasks", {}).items():
            if not cfg.get("enabled", True):
                continue
            schedule = cfg.get("schedule", "")
            if not schedule:
                continue
            try:
                cron_kwargs = _parse_cron(schedule)
                self.scheduler.add_job(
                    _run_nbs_task,
                    CronTrigger(**cron_kwargs),
                    args=[task_name, self.schedules_cfg],
                    id=f"nbs_{task_name}",
                    name=f"NBS [{task_name}]",
                    misfire_grace_time=3600,
                    replace_existing=True,
                )
                logger.info("[Scheduler] 注册 NBS 任务 [%s]  cron=%s", task_name, schedule)
            except Exception as e:
                logger.warning("[Scheduler] 注册 NBS[%s] 失败: %s", task_name, e)

    def _setup_agent_tasks(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        agent_funcs = {
            "heat":  _run_heat_agent,
            "news":  _run_news_agent,
            "quant": _run_quant_agent,
            # macro 由 NBS 任务事件触发，不走 cron
        }
        for name, func in agent_funcs.items():
            cfg = self.schedules_cfg.get("agents", {}).get(name, {})
            if not cfg.get("enabled", True):
                continue
            schedule = cfg.get("schedule", "")
            if not schedule:
                continue
            try:
                cron_kwargs = _parse_cron(schedule)
                self.scheduler.add_job(
                    func,
                    CronTrigger(**cron_kwargs),
                    id=f"agent_{name}",
                    name=f"Agent [{name}]",
                    misfire_grace_time=3600,
                    replace_existing=True,
                )
                logger.info("[Scheduler] 注册 Agent [%s]  cron=%s", name, schedule)
            except Exception as e:
                logger.warning("[Scheduler] 注册 Agent[%s] 失败: %s", name, e)

    def _setup_video_groups(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        for group, cfg in self.schedules_cfg.get("video_groups", {}).items():
            if not cfg.get("enabled", True):
                continue
            schedule = cfg.get("schedule", "")
            if not schedule:
                continue
            try:
                cron_kwargs = _parse_cron(schedule)
                self.scheduler.add_job(
                    _run_video_group,
                    CronTrigger(**cron_kwargs),
                    args=[group],
                    id=f"video_{group}",
                    name=f"Video [{cfg.get('name', group)}]",
                    misfire_grace_time=3600,
                    replace_existing=True,
                )
                logger.info(
                    "[Scheduler] 注册 Video Group [%s] (%s)  cron=%s",
                    group, cfg.get("name", ""), schedule,
                )
            except Exception as e:
                logger.warning("[Scheduler] 注册 Video[%s] 失败: %s", group, e)

    def _setup_run_all(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        cfg = self.schedules_cfg.get("run_all", {})
        if not cfg.get("enabled", True):
            return
        schedule = cfg.get("schedule", "")
        if not schedule:
            return
        try:
            cron_kwargs = _parse_cron(schedule)
            self.scheduler.add_job(
                _run_all,
                CronTrigger(**cron_kwargs),
                id="run_all",
                name="run-all [决策汇总]",
                misfire_grace_time=3600,
                replace_existing=True,
            )
            logger.info("[Scheduler] 注册 run-all  cron=%s", schedule)
        except Exception as e:
            logger.warning("[Scheduler] 注册 run-all 失败: %s", e)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def list_jobs(self) -> None:
        jobs = self.scheduler.get_jobs()
        if not jobs:
            print("（无已注册任务）")
            return
        print(f"\n{'任务ID':<25} {'名称':<30} {'下次执行时间'}")
        print("-" * 80)
        for job in jobs:
            next_run = str(job.next_run_time)[:19] if job.next_run_time else "（已暂停）"
            print(f"{job.id:<25} {job.name:<30} {next_run}")
        print()

    def run(self) -> None:
        """启动调度守护进程（阻塞运行，Ctrl+C 退出）"""
        self.list_jobs()
        logger.info("[Scheduler] 守护进程已启动（时区：Asia/Shanghai）")
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("[Scheduler] 收到停止信号，正在关闭…")
            self.scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    _log_dir = _PROJECT_ROOT / "data" / "cache"
    _log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_log_dir / "scheduler.log", encoding="utf-8"),
        ],
    )


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog="scheduler.py",
        description="行业板块 AI 研究平台 - 调度守护进程",
    )
    parser.add_argument("--list",    action="store_true", help="列出所有调度任务")
    parser.add_argument("--dry-run", action="store_true", help="验证配置，不实际启动")
    args = parser.parse_args()

    sched = AgentScheduler()

    if args.list or args.dry_run:
        sched.list_jobs()
        if args.dry_run:
            logger.info("[Scheduler] dry-run 模式：配置验证通过，未启动调度")
        return

    sched.run()


if __name__ == "__main__":
    main()
