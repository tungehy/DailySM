"""
行业板块 AI 研究平台 - 统一主入口

子命令：
  run-all   — 运行所有 Agent，生成综合决策日报（核心命令）
  heat      — 板块热度雷达（采集/计算/绘图），兼容 sector_heat_main.py
  video     — 视频日报流水线（B站下载 → 转录 → LLM 总结）
  news      — 单独运行新闻分析 Agent
  macro     — 单独运行宏观分析 Agent
  schedule  — 启动调度守护进程（APScheduler）

使用示例：
  python main.py run-all                       # 今日全量分析（自动复用有效缓存）
  python main.py run-all --date 2026-07-08     # 指定日期
  python main.py run-all --agents heat,news    # 只运行指定 Agent
  python main.py run-all --force               # 强制重新分析所有 Agent
  python main.py heat run                      # 热度雷达（采集+计算+绘图）
  python main.py heat backfill --start 2026-01-01 --end 2026-07-08
  python main.py video                         # 视频日报
  python main.py schedule                      # 启动定时调度守护进程
  python main.py schedule --list               # 列出所有调度任务
  python main.py notify-test                   # 向所有已启用渠道发送测试消息
  python main.py index fetch                   # 增量采集所有指数（自动补齐缺失日期）
  python main.py index backfill --start 2025-01-01  # 全量回填历史数据
  python main.py index list                    # 列出支持的指数
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# ---- 配置日志（其他模块依赖 config，需最先执行）----
_PROJECT_ROOT = Path(__file__).parent
_LOG_DIR      = _PROJECT_ROOT / "data" / "cache"
_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，强制 stdout/stderr 用 UTF-8，避免 ✓ 等字符崩溃"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _setup_logging(level: str = "INFO") -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level   = getattr(logging, level.upper(), logging.INFO),
        format  = fmt,
        datefmt = "%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_LOG_DIR / "run.log", encoding="utf-8"),
        ],
    )


_force_utf8_stdout()
_setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 子命令：run-all
# ---------------------------------------------------------------------------

def _load_schedules_cfg() -> dict:
    """加载 config/schedules.yaml，失败返回空字典"""
    import yaml
    schedules_path = _PROJECT_ROOT / "config" / "schedules.yaml"
    try:
        with open(schedules_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.debug("schedules.yaml 加载失败: %s", e)
        return {}


def cmd_run_all(args) -> bool:
    """
    运行所有（或指定）Agent，生成综合决策日报。

    freshness check 逻辑：
      - 对每个 Agent，先检查 analysis_results 中是否有仍有效的缓存结果。
      - 若有效且 --force 未设置 → 直接复用缓存，跳过重新分析。
      - 若无效或 --force → 重新运行 Agent.analyze()。
      - MacroAgent / VideoAgent 已内置自己的事件驱动缓存逻辑，无需在此额外处理。
    """
    from agents.base           import BaseAgent
    from agents.heat_agent     import HeatAgent
    from agents.macro_agent    import MacroAgent
    from agents.news_agent     import NewsAgent
    from agents.video_agent    import VideoAgent
    from agents.quant_agent    import QuantAgent
    from agents.research_agent import ResearchAgent
    from agents.decision_agent import DecisionAgent
    from skills.analysis_repo  import init_analysis_table, load_latest_as_result

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    enabled     = set(args.agents.split(",")) if args.agents else {"heat", "macro", "news", "video", "quant"}
    force       = getattr(args, "force", False)

    logger.info("=== 行业板块 AI 研究平台 | %s ===", target_date)

    # 加载调度配置（用于读取各 Agent 的 expire_hours）
    schedules_cfg = _load_schedules_cfg()
    agents_sched  = schedules_cfg.get("agents", {})

    # 初始化 DB 引擎和 Analysis Repository 表
    try:
        from sector_heat.db import get_engine
        engine = get_engine()
        init_analysis_table(engine)
    except Exception as e:
        logger.warning("Analysis Repository 初始化失败: %s，将不使用缓存", e)
        engine = None

    # 构建 Agent 列表（video 使用 group_a 作为 run-all 时的默认分组）
    # research 已接入统一观点库，但数据源待接入（is_available()=False，默认自动跳过）
    all_agents = {
        "heat":     HeatAgent(),
        "macro":    MacroAgent(skip_nbs=getattr(args, "skip_nbs", False)),
        "news":     NewsAgent(),
        "video":    VideoAgent(group="group_a", run_pipeline=getattr(args, "run_pipeline", False)),
        "quant":    QuantAgent(),
        "research": ResearchAgent(),
    }

    agents_to_run = [(name, agent) for name, agent in all_agents.items() if name in enabled]
    logger.info("启用 Agent：%s", [n for n, _ in agents_to_run])

    results = []
    for name, agent in agents_to_run:
        expire_h      = int(agents_sched.get(name, {}).get("expire_hours", 24))
        analysis_type = f"video_group_a" if name == "video" else ""

        # --- freshness check ---
        if not force and engine is not None:
            try:
                if BaseAgent.check_fresh(engine, name, analysis_type, expire_h):
                    cached = load_latest_as_result(engine, name, analysis_type)
                    if cached:
                        logger.info(
                            "  [%s] 使用缓存分析结果（有效期=%dh，置信度=%.0f%%）",
                            name, expire_h, cached.confidence * 100,
                        )
                        results.append(cached)
                        continue
            except Exception as e:
                logger.debug("[%s] freshness check 失败: %s", name, e)

        # --- 重新分析 ---
        if not agent.is_available():
            logger.warning("[%s] Agent 不可用，跳过", name)
            continue
        logger.info("▶ 运行 %s Agent（force=%s）…", name, force)
        try:
            result = agent.analyze(target_date)
            results.append(result)
            logger.info(
                "  ✓ %s 完成（置信度=%.0f%%，板块=%d个）",
                name, result.confidence * 100, len(result.top_sectors),
            )
        except Exception as e:
            logger.error("  ✗ %s 失败: %s", name, e)

    if not results:
        logger.error("所有 Agent 均失败，无法生成报告")
        return False

    # 决策 Agent 聚合
    logger.info("▶ 运行 Decision Agent（聚合 %d 个结果）…", len(results))
    decision = DecisionAgent(save_report=True)
    final    = decision.aggregate(results, trade_date=target_date)

    # 决策结果也保存到 Analysis Repository
    if engine is not None:
        try:
            from skills.analysis_repo import save_result as _save_ar
            _save_ar(engine, final, analysis_type="decision", expire_hours=20)
        except Exception as e:
            logger.debug("保存 decision 到 analysis_results 失败: %s", e)

    logger.info(
        "✓ 决策报告已生成：前5推荐板块 = %s",
        "、".join(s["sector"] for s in final.top_sectors[:5]),
    )
    print("\n" + "=" * 60)
    print(f"行业板块 AI 研究平台 | 今日推荐（{target_date}）")
    print("=" * 60)
    for i, s in enumerate(final.top_sectors[:8], 1):
        direction_tag = "[多]" if s["direction"] == "bullish" else ("[空]" if s["direction"] == "bearish" else "[中]")
        print(f"  {direction_tag} {i:2d}. {s['sector']:<12} 综合分 {s['score']:.0f}")
    print("=" * 60)
    print(f"报告已保存至 data/reports/{target_date}-decision.md")
    return True


# ---------------------------------------------------------------------------
# 子命令：heat（转发给 sector_heat_main）
# ---------------------------------------------------------------------------

def cmd_heat(args) -> bool:
    """板块热度雷达子命令（直接委托 sector_heat_main）"""
    import sector_heat_main
    # 重新构建 sys.argv 传给 sector_heat_main
    sub_argv = [sector_heat_main.__file__] + (args.heat_args or [])
    old_argv = sys.argv
    sys.argv  = sub_argv
    try:
        sector_heat_main.main()
        return True
    except SystemExit as e:
        return e.code == 0
    except Exception as e:
        logger.error("[heat] 执行失败: %s", e)
        return False
    finally:
        sys.argv = old_argv


# ---------------------------------------------------------------------------
# 子命令：video（视频日报流水线）
# ---------------------------------------------------------------------------

def cmd_video(args) -> bool:
    """视频日报：B站下载 → Whisper 转录 → LLM 总结"""
    target_date = args.date or getattr(args, "date_flag", None) or str(date.today())
    dry_run     = getattr(args, "dry_run", False)
    logger.info("=== 视频日报流水线 | %s ===", target_date)

    try:
        from pipeline.config      import config as cfg
        cfg.load()
        from pipeline.fetcher     import BilibiliFetcher
        from pipeline.downloader  import AudioDownloader
        from pipeline.transcriber import WhisperTranscriber
        from pipeline.summarizer  import MarketSummarizer
        from pipeline.reporter    import ReportWriter
        from pipeline.reporter    import StateDB  # 建表用，无需传递给其他类

        from pipeline.summarizer import VideoTranscriptPair

        fetcher     = BilibiliFetcher()
        downloader  = AudioDownloader()
        transcriber = WhisperTranscriber()
        summarizer  = MarketSummarizer()
        writer      = ReportWriter()
        _StateDB    = StateDB   # 可选：内部状态记录，构造即自动建表

        videos = fetcher.fetch_all_hosts()
        logger.info("获取到 %d 个视频", len(videos))
        if not videos:
            logger.warning("无新视频可处理")
            return True
        if dry_run:
            for v in videos:
                print(f"  [dry-run] {v.title}")
            return True

        # Step 1: 下载音频
        download_results = downloader.download_batch(videos)
        video_by_bvid    = {v.bvid: v for v in videos}

        # Step 2: 转录（只处理成功下载的）
        tasks = [
            (r.bvid, r.audio_path)
            for r in download_results
            if r.success and r.audio_path
        ]
        if not tasks:
            logger.error("所有视频下载失败，无法转录")
            return False

        transcript_results = transcriber.transcribe_batch(tasks)

        # Step 3: 配对 VideoInfo + TranscriptResult
        pairs = [
            VideoTranscriptPair(video=video_by_bvid[t.bvid], transcript=t)
            for t in transcript_results
            if t.success and t.bvid in video_by_bvid
        ]
        if not pairs:
            logger.error("所有音频转录失败，无法生成报告")
            return False

        # Step 4: LLM 总结
        report_date = date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
        summary = summarizer.summarize(pairs, report_date=report_date)

        if summary and summary.success:
            writer.write(summary)
            logger.info("✓ 日报已保存: data/reports/%s.md", target_date)
            return True
        else:
            err = summary.error if summary else "未知错误"
            logger.warning("LLM 未生成有效报告: %s", err)
            return False
    except Exception as e:
        logger.error("[video] 失败: %s", e, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 子命令：news（单独运行新闻 Agent）
# ---------------------------------------------------------------------------

def cmd_news(args) -> bool:
    """单独运行新闻分析 Agent，输出板块影响分析"""
    from agents.news_agent import NewsAgent

    date_str    = args.date or getattr(args, "date_flag", None)
    target_date = date.fromisoformat(date_str) if date_str else date.today()
    force       = getattr(args, "force", False)
    agent       = NewsAgent(categories=["finance", "news", "social"])
    result      = agent.analyze(target_date, force=force)

    print(f"\n{'='*60}")
    print(f"📰 新闻流量分析（{target_date}）")
    print(f"{'='*60}")
    print(f"置信度：{result.confidence:.0%}  模式：{result.mode}")
    print("\n受益板块：")
    for s in [x for x in result.top_sectors if x["direction"] == "bullish"]:
        print(f"  🔴 {s['sector']:<12} {s['reason'][:40]}")
    print("\n承压板块：")
    for s in [x for x in result.top_sectors if x["direction"] == "bearish"]:
        print(f"  🔵 {s['sector']:<12} {s['reason'][:40]}")
    return True


# ---------------------------------------------------------------------------
# 子命令：macro（单独运行宏观 Agent）
# ---------------------------------------------------------------------------

def cmd_macro(args) -> bool:
    """单独运行宏观分析 Agent（事件驱动：如数据未更新则复用缓存）"""
    from agents.macro_agent import MacroAgent

    date_str    = args.date or getattr(args, "date_flag", None)
    target_date = date.fromisoformat(date_str) if date_str else date.today()
    force       = getattr(args, "force", False)
    agent       = MacroAgent(skip_nbs=getattr(args, "skip_nbs", False))
    result = agent.analyze(target_date, force=force)

    print(f"\n{'='*60}")
    print(f"📈 宏观分析（{target_date}）")
    print(f"{'='*60}")
    print(f"置信度：{result.confidence:.0%}")
    print("\n看多板块：")
    for s in result.bullish_sectors:
        print(f"  🔴 {s['sector']:<12} {s['reason'][:50]}")
    print("\n看空板块：")
    for s in result.bearish_sectors:
        print(f"  🔵 {s['sector']:<12} {s['reason'][:50]}")
    return True


# ---------------------------------------------------------------------------
# 子命令：index（市场指数数据采集）
# ---------------------------------------------------------------------------

def cmd_index(args) -> bool:
    """市场指数数据采集（上证/深证/创业板/科创50/恒生/恒生科技）"""
    from skills.index_skill import IndexSkill
    from sector_heat.db import get_engine

    sub = getattr(args, "index_cmd", "fetch")

    if sub == "list":
        skill = IndexSkill()
        indices = skill.get_index_list()
        print(f"\n{'代码':<12} {'名称':<14} {'市场'}")
        print("-" * 36)
        for idx in indices:
            print(f"  {idx['code']:<12} {idx['name']:<14} {'A股' if idx['market'] == 'cn' else '港股'}")
        print()
        return True

    engine      = get_engine()
    skill       = IndexSkill(interval=1.0)
    start_date  = date.fromisoformat(args.start) if getattr(args, "start", None) else None
    end_date    = date.fromisoformat(args.end)   if getattr(args, "end",   None) else date.today()
    incremental = (sub == "fetch")   # backfill 不走增量逻辑

    if sub == "backfill":
        if not start_date:
            logger.error("backfill 需要 --start 参数，例如 --start 2025-01-01")
            return False
        logger.info("=== 指数数据回填 %s ~ %s ===", start_date, end_date)
        n = skill.fetch_and_store(engine, start_date, end_date, incremental=False)
    else:
        logger.info("=== 增量更新所有指数（截至 %s）===", end_date)
        n = skill.fetch_and_store(engine, start_date, end_date, incremental=True)

    print(f"\n指数数据采集完成，共写入 {n} 条记录")
    return True


# ---------------------------------------------------------------------------
# 子命令：notify-test（通知渠道测试）
# ---------------------------------------------------------------------------

def cmd_notify_test(args) -> bool:
    """向所有已启用的通知渠道发送测试消息，验证 Webhook 配置是否正确"""
    from skills.notification import NotificationCenter

    nc      = NotificationCenter()
    results = nc.test_all_channels()

    print(f"\n{'='*50}")
    print("通知中心 Webhook 连通性测试")
    print(f"{'='*50}")
    if not results:
        print("  （无已配置渠道）")
        print(f"\n请在 config.yaml 的 notifications.channels 下配置 Webhook URL。")
        return True

    all_ok = True
    for ch_name, ok in results.items():
        if ok is None:
            print(f"  [{ch_name:12}]  ⊘ 未启用（enabled=false 或 webhook 为空）")
        elif ok:
            print(f"  [{ch_name:12}]  ✓ 发送成功")
        else:
            print(f"  [{ch_name:12}]  ✗ 发送失败（请检查 Webhook URL 和网络）")
            all_ok = False
    print(f"{'='*50}\n")
    return all_ok


# ---------------------------------------------------------------------------
# 子命令：schedule（调度守护进程）
# ---------------------------------------------------------------------------

def cmd_schedule(args) -> bool:
    """启动 APScheduler 调度守护进程，或列出已注册任务"""
    try:
        import scheduler as sched_module
    except ImportError:
        logger.error("scheduler.py 未找到，请确认文件存在于项目根目录")
        return False

    scheduler = sched_module.AgentScheduler()

    if getattr(args, "list", False):
        scheduler.list_jobs()
        return True

    if getattr(args, "dry_run", False):
        scheduler.list_jobs()
        logger.info("[schedule] dry-run 模式：配置验证通过，未启动调度")
        return True

    logger.info("[schedule] 启动调度守护进程（Ctrl+C 停止）…")
    scheduler.run()
    return True


# ---------------------------------------------------------------------------
# 子命令：research（研报 RAG 知识库）
# ---------------------------------------------------------------------------

def cmd_research(args) -> bool:
    """研报知识库：导入 / 检索 / 列表 / 运行分析"""
    from sector_heat.db import get_engine
    from skills.research_db import init_research_tables

    sub_cmd = getattr(args, "research_cmd", None)
    engine  = get_engine()
    init_research_tables(engine)

    # ---- import ----
    if sub_cmd == "import":
        from pathlib import Path
        from skills.doc_parser import parse_file
        from skills.research_db import insert_report, count_reports
        from skills.embedding_client import EmbeddingClient

        p = Path(args.path)
        if not p.exists():
            logger.error("[research] 路径不存在: %s", p)
            return False

        # 收集待导入文件（单文件或目录批量）
        exts = {".docx", ".pdf", ".md", ".markdown", ".json", ".txt", ".text"}
        files = (
            [f for f in sorted(p.iterdir()) if f.suffix.lower() in exts]
            if p.is_dir() else [p]
        )
        if not files:
            logger.warning("[research] 未找到可导入文件（支持 %s）", "/".join(sorted(exts)))
            return False

        overrides = {
            "report_type": args.report_type,
            "institution": args.institution,
            "analyst":     args.analyst,
            "publish_time": args.pub_date,
        }
        if args.industries:
            overrides["industries"] = [
                x.strip() for x in args.industries.replace("，", ",").split(",") if x.strip()
            ]
        overrides = {k: v for k, v in overrides.items() if v}

        embed = EmbeddingClient()
        ok, skip = 0, 0
        for f in files:
            try:
                parsed = parse_file(f)
                # CLI 显式参数优先覆盖文件内元数据（仅当文件内缺失或用户强制）
                for k, v in overrides.items():
                    if v and not parsed.get(k):
                        parsed[k] = v
                res = insert_report(engine, embed, parsed)
                if res["skipped"]:
                    skip += 1
                    print(f"  ⊘ 已存在跳过: {f.name}")
                else:
                    ok += 1
                    print(f"  ✓ 导入成功: {f.name}（{res['chunks']} 块，id={res['report_id']}）")
            except Exception as e:
                logger.error("[research] 导入失败 %s: %s", f.name, e)

        stat = count_reports(engine)
        print(f"\n导入完成：成功 {ok}，跳过 {skip}。"
              f"知识库现有 {stat['reports']} 篇研报 / {stat['chunks']} 块。")
        return ok > 0 or skip > 0

    # ---- list ----
    if sub_cmd == "list":
        from skills.research_db import list_reports, count_reports
        reports = list_reports(engine)
        stat = count_reports(engine)
        print(f"\n{'='*70}\n研报知识库（{stat['reports']} 篇 / {stat['chunks']} 块）\n{'='*70}")
        if not reports:
            print("  （空，请用 python main.py research import <文件> 导入）")
            return True
        for r in reports:
            inds = "、".join(r.get("industries") or []) or "-"
            pub  = r["publish_time"].strftime("%Y-%m-%d") if r.get("publish_time") else "?"
            print(f"  [{r['id']:>3}] {pub} | {r.get('report_type') or '未分类':14} "
                  f"| {(r.get('institution') or '-'):8} | {inds}")
            print(f"        {r['title'][:60]}（{r['chunk_count']} 块）")
        print(f"{'='*70}\n")
        return True

    # ---- search ----
    if sub_cmd == "search":
        from skills.research_db import search
        from skills.embedding_client import EmbeddingClient
        embed = EmbeddingClient()
        hits = search(engine, embed, args.query,
                      top_k=args.top_k, industry=args.industry,
                      drop_threshold=0.0)   # 检索测试不丢弃，展示衰减
        print(f"\n检索：「{args.query}」" + (f"（行业={args.industry}）" if args.industry else ""))
        print(f"{'='*70}")
        if not hits:
            print("  无匹配结果（知识库为空？）")
            return True
        for i, h in enumerate(hits):
            print(f"\n#{i+1}  综合分={h['score']:.3f}  "
                  f"(相似度={h['similarity']:.3f} × 时效={h['decay']:.3f})")
            print(f"    来源: {h.get('institution') or '-'} | {h.get('report_title') or '-'}")
            print(f"    {h['content'][:150]}…")
        print(f"\n{'='*70}\n")
        return True

    # ---- run ----
    if sub_cmd == "run":
        from agents.research_agent import ResearchAgent
        target = date.fromisoformat(args.date) if args.date else date.today()
        agent  = ResearchAgent()
        if not agent.is_available():
            logger.warning("[research] 知识库无新鲜研报，请先 import")
            return False
        result = agent.analyze(target)
        print("\n" + result.summary + "\n")
        return result.confidence > 0

    logger.error("[research] 未知子命令: %s", sub_cmd)
    return False


# ---------------------------------------------------------------------------
# 子命令：chat（交互式对话决策 Agent）
# ---------------------------------------------------------------------------

def cmd_chat(args) -> bool:
    """交互式对话决策：意图理解 → 按需补数据 → 融合持仓给出个性化建议"""
    from agents.chat_agent import ChatAgent
    from sector_heat.db import init_db

    init_db()   # 确保 user_portfolio / chat_history 等表存在
    agent = ChatAgent()

    # 单次提问模式
    ask = getattr(args, "ask", None)
    if ask:
        print("\n" + agent.chat(ask) + "\n")
        return True

    # 交互式 REPL
    print("=" * 60)
    print("  A股行业对话决策助手（输入 exit / quit 退出）")
    print("  会自动结合：实时新闻 + 各Agent分析 + 统一观点库 + 你的持仓")
    print("=" * 60)
    while True:
        try:
            user_input = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "退出", "q"):
            print("再见。")
            break
        print("\n助手 > 思考中…")
        answer = agent.chat(user_input)
        print("\n助手 > " + answer)
    return True


# ---------------------------------------------------------------------------
# 子命令：portfolio（持仓管理）
# ---------------------------------------------------------------------------

def cmd_portfolio(args) -> bool:
    """用户持仓管理：list / add / remove / clear"""
    from sector_heat.db import get_engine
    from skills.portfolio_db import (
        init_portfolio_table, get_holdings, upsert_holding,
        delete_holding, clear_holdings, format_holdings,
    )
    engine = get_engine()
    init_portfolio_table(engine)

    sub_cmd = getattr(args, "portfolio_cmd", None) or "list"

    if sub_cmd == "list":
        holdings = get_holdings(engine)
        print("\n=== 当前持仓 ===")
        print(format_holdings(holdings))
        print()
        return True

    if sub_cmd == "add":
        upsert_holding(engine, {
            "name":         args.name,
            "holding_type": args.type,
            "position_pct": args.position,
            "cost_price":   args.cost,
            "shares":       args.shares,
            "note":         args.note,
        })
        print(f"已记录/更新持仓：{args.name}")
        return True

    if sub_cmd == "remove":
        n = delete_holding(engine, args.name)
        print(f"已删除 {n} 条持仓：{args.name}")
        return n > 0

    if sub_cmd == "clear":
        n = clear_holdings(engine)
        print(f"已清空 {n} 条持仓")
        return True

    logger.error("[portfolio] 未知子命令: %s", sub_cmd)
    return False


# ---------------------------------------------------------------------------
# 子命令：serve（Web 服务：前端 + API）
# ---------------------------------------------------------------------------

def cmd_serve(args) -> bool:
    """启动 FastAPI：托管构建后的前端（web/dist）+ /api 数据接口"""
    import uvicorn
    from pathlib import Path
    dist = Path(__file__).resolve().parent / "web" / "dist"
    if not dist.exists():
        logger.warning("前端未构建：%s 不存在。先执行 `cd web && npm run build`，"
                       "或开发时单独运行 `cd web && npm run dev`。", dist)
    logger.info("启动 Web 服务: http://%s:%d", args.host, args.port)
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return True


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog        = "main.py",
        description = "行业板块 AI 研究平台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run-all ---
    p_run = sub.add_parser("run-all", help="运行所有 Agent，生成综合决策日报")
    p_run.add_argument("--date",          help="交易日 YYYY-MM-DD（默认今日）")
    p_run.add_argument("--agents",        help="指定 Agent，逗号分隔，如 heat,news（默认全部）")
    p_run.add_argument("--skip-nbs",      action="store_true", help="跳过统计局爬虫（加快速度）")
    p_run.add_argument("--run-pipeline",  action="store_true", help="若无视频日报则触发 pipeline")
    p_run.add_argument("--force",         action="store_true", help="强制重新分析（忽略缓存）")

    # --- heat（pass-through 到 sector_heat_main）---
    p_heat = sub.add_parser("heat", help="板块热度雷达（委托 sector_heat_main）")
    p_heat.add_argument("heat_args", nargs=argparse.REMAINDER, help="传给 sector_heat_main 的参数")

    # --- video ---
    p_vid = sub.add_parser("video", help="视频日报流水线")
    p_vid.add_argument("date",     nargs="?", default=None, help="日期 YYYY-MM-DD（默认今日）")
    p_vid.add_argument("--date",   dest="date_flag", default=None, help="日期 YYYY-MM-DD（--date 写法）")
    p_vid.add_argument("--dry-run", action="store_true", help="只列出视频，不下载")

    # --- news ---
    p_news = sub.add_parser("news", help="单独运行新闻分析 Agent")
    p_news.add_argument("date",    nargs="?", default=None, help="日期 YYYY-MM-DD（默认今日）")
    p_news.add_argument("--date",  dest="date_flag", default=None, help="日期 YYYY-MM-DD（--date 写法）")
    p_news.add_argument("--force", action="store_true", help="强制重爬+重分析（忽略已完成状态）")

    # --- macro ---
    p_macro = sub.add_parser("macro", help="单独运行宏观分析 Agent")
    p_macro.add_argument("date",      nargs="?", default=None, help="日期 YYYY-MM-DD（默认今日）")
    p_macro.add_argument("--date",    dest="date_flag", default=None, help="日期 YYYY-MM-DD（--date 写法）")
    p_macro.add_argument("--skip-nbs", dest="skip_nbs", action="store_true")
    p_macro.add_argument("--force",    action="store_true", help="强制重新分析（忽略 NBS 数据版本缓存）")

    # --- schedule ---
    p_sched = sub.add_parser("schedule", help="启动/管理调度守护进程")
    p_sched.add_argument("--list",    action="store_true", help="列出所有调度任务及下次执行时间")
    p_sched.add_argument("--dry-run", action="store_true", help="验证配置，不实际启动调度")

    # --- index ---
    p_idx = sub.add_parser("index", help="市场指数数据采集（上证/深证/创业板/科创50/恒生/恒生科技）")
    idx_sub = p_idx.add_subparsers(dest="index_cmd", required=False)
    idx_sub.default = "fetch"

    idx_fetch = idx_sub.add_parser("fetch", help="增量更新（自动补齐最新缺失日期）")
    idx_fetch.add_argument("--end", help="截止日期 YYYY-MM-DD（默认今日）")

    idx_bf = idx_sub.add_parser("backfill", help="全量回填历史数据")
    idx_bf.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    idx_bf.add_argument("--end",   help="截止日期 YYYY-MM-DD（默认今日）")

    idx_sub.add_parser("list", help="列出支持的指数")

    # --- research（研报 RAG 知识库）---
    p_res = sub.add_parser("research", help="研报 RAG 知识库（导入/检索/分析）")
    res_sub = p_res.add_subparsers(dest="research_cmd", required=True)

    res_imp = res_sub.add_parser("import", help="手动导入研报（docx/pdf/md/json/txt）")
    res_imp.add_argument("path", help="研报文件路径，或目录（批量导入）")
    res_imp.add_argument("--type", dest="report_type", default=None,
                         help="报告类型：macro_strategy/industry_deep/company_note/data_flash")
    res_imp.add_argument("--institution", default=None, help="机构/券商")
    res_imp.add_argument("--analyst",     default=None, help="分析师")
    res_imp.add_argument("--date",        dest="pub_date", default=None,
                         help="发布日期 YYYY-MM-DD（默认取文件内或今日）")
    res_imp.add_argument("--industries",  default=None,
                         help="涉及行业，逗号分隔，如 半导体,医药生物")

    res_search = res_sub.add_parser("search", help="时效加权检索测试")
    res_search.add_argument("query", help="检索问题")
    res_search.add_argument("--industry", default=None, help="限定行业")
    res_search.add_argument("--top-k",    dest="top_k", type=int, default=6)

    res_run = res_sub.add_parser("run", help="运行研报 Agent（抽取观点入库）")
    res_run.add_argument("date", nargs="?", default=None, help="日期 YYYY-MM-DD（默认今日）")

    res_sub.add_parser("list", help="列出已导入研报")

    # --- chat（交互式对话决策）---
    p_chat = sub.add_parser("chat", help="交互式对话决策助手（自动补数据+融合持仓）")
    p_chat.add_argument("--ask", default=None, help="单次提问（不进入交互模式）")

    # --- portfolio（持仓管理）---
    p_pf = sub.add_parser("portfolio", help="用户持仓管理")
    pf_sub = p_pf.add_subparsers(dest="portfolio_cmd", required=False)
    pf_sub.default = "list"
    pf_sub.add_parser("list", help="查看当前持仓")
    pf_add = pf_sub.add_parser("add", help="添加/更新持仓")
    pf_add.add_argument("name", help="标的名，如 半导体 / 贵州茅台")
    pf_add.add_argument("--type", default="sector", help="sector/stock/etf/theme")
    pf_add.add_argument("--position", type=float, default=None, help="仓位占比 %%")
    pf_add.add_argument("--cost",     type=float, default=None, help="成本价")
    pf_add.add_argument("--shares",   type=float, default=None, help="持股数")
    pf_add.add_argument("--note",     default=None, help="备注")
    pf_rm = pf_sub.add_parser("remove", help="删除持仓")
    pf_rm.add_argument("name", help="标的名")
    pf_sub.add_parser("clear", help="清空全部持仓")

    # --- notify-test ---
    sub.add_parser("notify-test", help="向所有已启用渠道发送测试消息，验证 Webhook 配置")

    # --- serve（前端 + API）---
    p_serve = sub.add_parser("serve", help="启动 Web 服务（托管前端 + /api）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve.add_argument("--port", type=int, default=8000, help="监听端口")
    p_serve.add_argument("--reload", action="store_true", help="开发模式（热重载）")

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    # 重新设置日志级别
    logging.getLogger().setLevel(getattr(logging, args.log_level.upper(), logging.INFO))

    dispatch = {
        "run-all":     cmd_run_all,
        "heat":        cmd_heat,
        "video":       cmd_video,
        "news":        cmd_news,
        "macro":       cmd_macro,
        "schedule":    cmd_schedule,
        "notify-test": cmd_notify_test,
        "index":       cmd_index,
        "research":    cmd_research,
        "chat":        cmd_chat,
        "portfolio":   cmd_portfolio,
        "serve":       cmd_serve,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    success = handler(args)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
