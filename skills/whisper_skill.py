"""
Whisper 语音转文字 Skill

封装 pipeline/transcriber.py，
提供音频转录的统一接口，自动处理缓存。
Agent 层无需直接依赖 pipeline 内部模块。

能力：
  transcribe(bvid, audio_path)     — 单文件转录（自动缓存）
  transcribe_batch(download_results) — 批量转录
  read_transcript(bvid)            — 读取已有转录文本
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_pipeline_ready = False


def _ensure_pipeline():
    global _pipeline_ready
    if not _pipeline_ready:
        from pipeline.config import config
        config.load()
        _pipeline_ready = True


class WhisperSkill:
    """
    Whisper 语音转文字 Skill

    整合 WhisperTranscriber + 缓存检查。
    模型单例由 WhisperTranscriber 内部管理（延迟加载）。
    """

    def __init__(self):
        _ensure_pipeline()

    # ------------------------------------------------------------------
    # 转录
    # ------------------------------------------------------------------

    def transcribe(self, bvid: str, audio_path: Path) -> object:
        """
        转录单个音频文件。已有缓存则直接返回。

        Args:
            bvid:       B站视频 BVID
            audio_path: 音频文件路径

        Returns:
            TranscriptResult
        """
        _ensure_pipeline()
        from pipeline.transcriber import WhisperTranscriber
        try:
            result = WhisperTranscriber().transcribe(bvid, audio_path)
            logger.debug(
                "[WhisperSkill] 转录 %s 完成（%d 字）",
                bvid, len(result.full_text),
            )
            return result
        except Exception as e:
            logger.error("[WhisperSkill] transcribe 失败 bvid=%s: %s", bvid, e)
            from pipeline.transcriber import TranscriptResult
            return TranscriptResult(
                bvid=bvid, audio_path=audio_path,
                transcript_path=None, full_text="",
                success=False, error=str(e),
            )

    def transcribe_batch(self, download_results: list) -> list:
        """
        批量转录（从 DownloadResult 列表中过滤出成功的下载）。

        Args:
            download_results: list[DownloadResult]（来自 BilibiliSkill.download_batch）

        Returns:
            list[TranscriptResult]
        """
        _ensure_pipeline()
        from pipeline.transcriber import WhisperTranscriber

        tasks = [
            (r.bvid, r.audio_path)
            for r in download_results
            if r.success and r.audio_path
        ]
        if not tasks:
            logger.warning("[WhisperSkill] 无可转录的音频文件")
            return []

        try:
            results = WhisperTranscriber().transcribe_batch(tasks)
            ok = sum(1 for r in results if r.success)
            logger.info("[WhisperSkill] 批量转录：%d/%d 成功", ok, len(tasks))
            return results
        except Exception as e:
            logger.error("[WhisperSkill] transcribe_batch 失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def read_transcript(self, bvid: str) -> str:
        """读取已缓存的转录文本（不存在则返回空字符串）"""
        _ensure_pipeline()
        from pipeline.config import config
        txt_path = Path(config.transcript_dir) / f"{bvid}.txt"
        if txt_path.exists():
            return txt_path.read_text(encoding="utf-8").strip()
        return ""

    def get_transcript_path(self, bvid: str) -> Path | None:
        """返回转录缓存文件路径（不存在则返回 None）"""
        _ensure_pipeline()
        from pipeline.config import config
        txt_path = Path(config.transcript_dir) / f"{bvid}.txt"
        return txt_path if txt_path.exists() else None
