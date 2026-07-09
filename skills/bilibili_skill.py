"""
B 站数据获取 Skill

封装 pipeline/fetcher.py + pipeline/downloader.py，
提供视频列表获取和音频下载的统一接口。
Agent 层无需直接依赖 pipeline 内部模块。

能力：
  fetch_videos(days_filter)    — 获取所有目标 UP 主的最新视频列表
  download(video)              — 下载单个视频音频
  download_batch(videos)       — 批量下载
  is_downloaded(bvid)          — 查询视频是否已下载
  mark_downloaded(bvid, path)  — 标记已下载
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 延迟导入 pipeline（pipeline 使用 config 单例，需在 config.load() 后才可用）
_pipeline_ready = False


def _ensure_pipeline():
    """确保 pipeline config 已加载"""
    global _pipeline_ready
    if not _pipeline_ready:
        from pipeline.config import config
        config.load()
        _pipeline_ready = True


class BilibiliSkill:
    """
    B 站数据获取 Skill

    整合 BilibiliFetcher + AudioDownloader + StateDB，
    提供简洁的 fetch → download 工作流。
    """

    def __init__(self):
        _ensure_pipeline()
        from pipeline.reporter import StateDB
        self._state_db = StateDB()

    # ------------------------------------------------------------------
    # 视频列表获取
    # ------------------------------------------------------------------

    def fetch_videos(self, days_filter: int | None = None) -> list:
        """
        获取所有目标 UP 主符合条件的最新视频列表。

        Args:
            days_filter: 只返回最近 N 天内发布的视频；
                         None = 使用 config.bilibili.days_filter

        Returns:
            list[VideoInfo]
        """
        _ensure_pipeline()
        from pipeline.fetcher import BilibiliFetcher
        from pipeline.config  import config

        try:
            with BilibiliFetcher() as fetcher:
                # 临时覆盖 days_filter（如需指定）
                orig = None
                if days_filter is not None:
                    orig = config.days_filter
                    config._cfg["bilibili"]["days_filter"] = days_filter

                videos = fetcher.fetch_all_hosts()

                if orig is not None:
                    config._cfg["bilibili"]["days_filter"] = orig

            logger.info("[BilibiliSkill] 获取到 %d 个视频", len(videos))
            return videos
        except Exception as e:
            logger.error("[BilibiliSkill] fetch_videos 失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 音频下载
    # ------------------------------------------------------------------

    def download(self, video) -> object:
        """
        下载单个视频音频。

        Args:
            video: VideoInfo 对象

        Returns:
            DownloadResult
        """
        _ensure_pipeline()
        from pipeline.downloader import AudioDownloader
        try:
            result = AudioDownloader().download(video)
            if result.success:
                self._state_db.mark_downloaded(video.bvid, str(result.audio_path))
            return result
        except Exception as e:
            logger.error("[BilibiliSkill] download 失败 bvid=%s: %s", video.bvid, e)
            from pipeline.downloader import DownloadResult
            return DownloadResult(bvid=video.bvid, audio_path=None, success=False, error=str(e))

    def download_batch(self, videos: list) -> list:
        """
        批量下载音频，已下载的会自动跳过。

        Args:
            videos: list[VideoInfo]

        Returns:
            list[DownloadResult]
        """
        _ensure_pipeline()
        from pipeline.downloader import AudioDownloader
        try:
            results = AudioDownloader().download_batch(videos)
            for v, r in zip(videos, results):
                if r.success:
                    self._state_db.mark_downloaded(v.bvid, str(r.audio_path))
            logger.info(
                "[BilibiliSkill] 下载完成：%d/%d 成功",
                sum(r.success for r in results), len(results),
            )
            return results
        except Exception as e:
            logger.error("[BilibiliSkill] download_batch 失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def is_downloaded(self, bvid: str) -> bool:
        return self._state_db.is_downloaded(bvid)

    def is_transcribed(self, bvid: str) -> bool:
        return self._state_db.is_transcribed(bvid)

    def mark_transcribed(self, bvid: str, transcript_path: str) -> None:
        self._state_db.mark_transcribed(bvid, transcript_path)

    def mark_reported(self, bvid: str, report_date: str) -> None:
        self._state_db.mark_reported(bvid, report_date)

    def list_all_videos(self) -> list[dict]:
        return self._state_db.list_all()
