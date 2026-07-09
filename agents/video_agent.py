"""
视频观点 Agent —— Task Group 版

设计原则：
  - 支持多个独立 Task Group（group_a / group_b / group_c），各组有独立的
    调度时间、UP 主列表、Prompt 提示和有效期。
  - 每组分析完成后自动保存到 analysis_results（analysis_type = video_group_a 等）。
  - analyze() 先检查 analysis_results 是否有效缓存；若有效则直接返回，不重复分析。
  - run-all 不再触发视频 pipeline，改为直接读取最新有效分析结果。

工作流：
  1. 检查 analysis_results 中 video_{group} 的最新缓存是否有效（expire_hours 内）
  2. 若有效 → 直接返回缓存 AgentResult
  3. 若无效或强制 → 检查是否已有当日 Markdown 日报
  4. 若有日报 → 读取并 LLM 提取板块观点
  5. 若无日报且 run_pipeline=True → 完整流水线
  6. 分析完成后保存到 analysis_results
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from agents.base import AgentResult, BaseAgent
from skills.llm_client import LLMClient
from skills.bilibili_skill import BilibiliSkill
from skills.whisper_skill import WhisperSkill

logger = logging.getLogger(__name__)

_PROJECT_ROOT   = Path(__file__).parent.parent
_REPORT_DIR     = _PROJECT_ROOT / "data" / "reports"
_SCHEDULES_PATH = _PROJECT_ROOT / "config" / "schedules.yaml"


def _load_group_config(group: str) -> dict:
    """从 config/schedules.yaml 加载指定分组的配置"""
    try:
        with open(_SCHEDULES_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("video_groups", {}).get(group, {})
    except Exception:
        return {}


class VideoAgent(BaseAgent):
    """
    视频观点 Agent（Task Group 版）

    参数：
        group:        Task Group 名称，对应 config/schedules.yaml video_groups 中的 key
                      默认 "group_a"（每日博主观点）
        run_pipeline: True = 当报告不存在时自动触发完整下载→转录→生成流水线
        top_n:        输出板块数量
    """

    name        = "video"
    description = "视频观点（B站财经UP主 → Whisper转录 → LLM板块提取，支持Task Group）"

    def __init__(
        self,
        group:        str  = "group_a",
        run_pipeline: bool = False,
        top_n:        int  = 8,
    ):
        self.group         = group
        self.analysis_type = f"video_{group}"
        self.run_pipeline  = run_pipeline
        self.top_n         = top_n
        self._llm          = LLMClient()
        self._bili         = BilibiliSkill()
        self._whisper      = WhisperSkill()
        # 从配置读取有效期
        gcfg               = _load_group_config(group)
        self.expire_hours  = int(gcfg.get("expire_hours", 20))
        self.prompt_hint   = gcfg.get("prompt_hint", "")

    def is_available(self) -> bool:
        # 有缓存结果、有当日报告或允许 pipeline，均视为可用
        report_today = _REPORT_DIR / f"{date.today()}.md"
        if report_today.exists():
            return True
        try:
            from sector_heat.db import get_engine
            engine = get_engine()
            return self.check_fresh(engine, self.name, self.analysis_type, self.expire_hours)
        except Exception:
            pass
        return self.run_pipeline

    def analyze(self, trade_date: date, force: bool = False) -> AgentResult:
        """
        分析视频观点。

        缓存复用逻辑：
          1. 若 analysis_results 中有有效缓存（force=False）→ 直接返回
          2. 否则重新运行 pipeline 并保存结果
        """
        try:
            return self._run(trade_date, force=force)
        except Exception as e:
            logger.error("[VideoAgent/%s] 分析失败: %s", self.group, e, exc_info=True)
            return self._empty_result(trade_date, str(e))

    # ------------------------------------------------------------------
    # 内部主流程
    # ------------------------------------------------------------------

    def _run(self, trade_date: date, force: bool = False) -> AgentResult:
        from sector_heat.db import get_engine
        from skills.analysis_repo import init_analysis_table

        engine = get_engine()
        init_analysis_table(engine)

        # 1. 检查缓存（Task Group 独立缓存）
        if not force:
            cached = self.load_latest_result(engine, self.name, self.analysis_type)
            if cached and self.check_fresh(engine, self.name, self.analysis_type, self.expire_hours):
                logger.info(
                    "[VideoAgent/%s] 使用缓存分析结果（有效期=%dh）",
                    self.group, self.expire_hours,
                )
                return cached

        report_path = _REPORT_DIR / f"{trade_date}.md"

        # 2. 尝试读已有日报
        if report_path.exists():
            report_text = report_path.read_text(encoding="utf-8")
            logger.info("[VideoAgent/%s] 读取已有日报: %s", self.group, report_path.name)
            source = "cached_report"

        elif self.run_pipeline:
            logger.info("[VideoAgent/%s] 触发完整视频流水线…", self.group)
            report_text = self._run_pipeline(trade_date)
            source = "fresh_pipeline"
            if not report_text:
                return self._empty_result(trade_date, "pipeline 运行失败或无新视频")

        else:
            # 使用最近一份日报
            latest = self._find_latest_report()
            if latest:
                report_text = latest.read_text(encoding="utf-8")
                logger.warning(
                    "[VideoAgent/%s] 无当日报告，使用最近报告: %s", self.group, latest.name
                )
                source = f"fallback:{latest.stem}"
            else:
                return self._empty_result(
                    trade_date,
                    "无可用视频日报（请先运行 pipeline 或设置 run_pipeline=True）",
                )

        # 3. LLM 提取板块观点
        sector_view = self._extract_sectors(report_text)
        top_sectors = self._build_top_sectors(sector_view)

        # 4. 摘要
        gcfg       = _load_group_config(self.group)
        group_name = gcfg.get("name", self.group)
        hot_names  = "、".join(s["sector"] for s in top_sectors[:5] if s["direction"] == "bullish") or "暂无"
        cold_names = "、".join(s["sector"] for s in top_sectors if s["direction"] == "bearish")[:3] or "无"
        summary = (
            f"## 视频观点分析 - {group_name}（{trade_date}）\n\n"
            f"**信息来源**：B站财经UP主视频转录\n\n"
            f"**看多板块**：{hot_names}\n\n"
            f"**看空板块**：{cold_names}\n\n"
            f"### 原始报告摘要\n{report_text[:800]}…"
        )

        confidence = 0.7 if source in ("cached_report", "fresh_pipeline") else 0.4

        result = AgentResult(
            agent_name  = self.name,
            trade_date  = trade_date,
            summary     = summary,
            top_sectors = top_sectors,
            confidence  = confidence,
            mode        = f"bilibili_whisper_llm/{source}/{self.group}",
            raw_data    = {
                "group":         self.group,
                "report_file":   str(report_path),
                "sector_view":   sector_view,
                "report_preview":report_text[:600],
                "source":        source,
            },
        )

        # 5. 保存到 Analysis Repository（按 group 独立保存）
        self.save_result(result, engine,
                         analysis_type=self.analysis_type,
                         expire_hours=self.expire_hours)
        logger.info(
            "[VideoAgent/%s] 分析结果已保存（analysis_type=%s, expire=%dh）",
            self.group, self.analysis_type, self.expire_hours,
        )
        return result

    # ------------------------------------------------------------------
    # 完整 Pipeline 触发（使用 Skills）
    # ------------------------------------------------------------------

    def _run_pipeline(self, trade_date: date) -> str:
        """
        使用 BilibiliSkill + WhisperSkill 完整运行视频流水线，
        再用 LLMClient 生成 Markdown 日报，保存并返回文本。
        """
        # 1. 获取视频列表
        videos = self._bili.fetch_videos()
        if not videos:
            logger.warning("[VideoAgent] 无新视频")
            return ""

        # 2. 下载音频
        dl_results = self._bili.download_batch(videos)
        if not any(r.success for r in dl_results):
            logger.warning("[VideoAgent] 所有视频下载失败")
            return ""

        # 3. 转录
        tr_results = self._whisper.transcribe_batch(dl_results)
        if not tr_results:
            logger.warning("[VideoAgent] 所有视频转录失败")
            return ""

        # 4. 更新状态
        for r in tr_results:
            if r.success and r.transcript_path:
                self._bili.mark_transcribed(r.bvid, str(r.transcript_path))

        # 5. 用 LLM 生成日报
        report_text = self._generate_report(tr_results, trade_date)
        if not report_text:
            return ""

        # 6. 保存
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = _REPORT_DIR / f"{trade_date}.md"
        report_path.write_text(report_text, encoding="utf-8")
        logger.info("[VideoAgent] 日报已保存: %s", report_path.name)

        for r in tr_results:
            self._bili.mark_reported(r.bvid, str(trade_date))

        return report_text

    def _generate_report(self, transcripts: list, trade_date: date) -> str:
        """将转录结果合并，调用 LLM 生成板块行情日报"""
        # 构建转录文本（超长截断）
        sections = []
        for r in transcripts:
            if not r.success or not r.full_text:
                continue
            bvid_hint = r.bvid
            text = r.full_text[:3000]  # 每个视频最多 3000 字
            sections.append(f"【视频 {bvid_hint}】\n{text}")

        if not sections:
            return ""

        combined = "\n\n".join(sections)[:12000]
        prompt = (
            f"以下是{trade_date}当日A股财经UP主视频的转录内容，"
            "请整理成板块行情日报（Markdown格式）。\n\n"
            "要求：\n"
            "1. 提取各UP主对不同行业板块的观点（看多/看空/中性）\n"
            "2. 汇总当日市场主要话题和热点板块\n"
            "3. 整理成结构清晰的Markdown，包含【今日市场概述】【热点板块】【主要观点】三个部分\n"
            "4. 不涉及个股推荐，只分析板块\n\n"
            f"转录内容：\n{combined}"
        )
        return self._llm.sector_prompt(
            prompt,
            system="你是专业的A股板块行情日报整理员，擅长从视频转录中提炼板块投资观点。",
        )

    # ------------------------------------------------------------------
    # LLM 板块提取
    # ------------------------------------------------------------------

    def _extract_sectors(self, report_text: str) -> dict:
        """从日报文本中提取板块投资观点（JSON）"""
        prompt = f"""
以下是一份A股视频UP主观点整理的日报，请从中提取对行业板块的投资观点。

日报内容：
{report_text[:2500]}

请以JSON格式输出（只输出JSON，不要其他文字）：
{{
  "bullish_sectors": [
    {{"sector": "板块名（同花顺一级行业名称）", "reason": "UP主的具体看法", "confidence": 0.8}},
    ...
  ],
  "bearish_sectors": [
    {{"sector": "板块名", "reason": "理由", "confidence": 0.7}},
    ...
  ],
  "neutral_sectors": ["板块名1", "板块名2"],
  "market_view": "UP主对整体市场的整体看法（一句话）"
}}

注意：
- 板块名使用同花顺一级行业名称
- 只提取文中明确提及的板块
- confidence 基于UP主表述的确定性（0~1）
- 若文中无板块观点，返回空列表
"""
        result = self._llm.chat_json(
            [
                {"role": "system", "content": "你是A股板块分析助理，擅长从财经内容提取板块投资观点，只输出JSON。"},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=1200,
        )
        if not result:
            result = {"bullish_sectors": [], "bearish_sectors": [], "neutral_sectors": []}
        return result

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _find_latest_report(self) -> Path | None:
        reports = sorted(_REPORT_DIR.glob("????-??-??.md"), reverse=True)
        return reports[0] if reports else None

    def _build_top_sectors(self, sector_view: dict) -> list[dict]:
        results: list[dict] = []
        for item in sector_view.get("bullish_sectors", [])[:self.top_n]:
            conf = float(item.get("confidence", 0.6))
            results.append({
                "sector":    item.get("sector", ""),
                "score":     min(100, int(conf * 85 + 10)),
                "direction": "bullish",
                "reason":    item.get("reason", ""),
            })
        for item in sector_view.get("bearish_sectors", []):
            conf = float(item.get("confidence", 0.6))
            results.append({
                "sector":    item.get("sector", ""),
                "score":     max(0, int((1 - conf) * 50)),
                "direction": "bearish",
                "reason":    item.get("reason", ""),
            })
        return results
