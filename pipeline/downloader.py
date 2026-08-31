"""
视频/音频下载模块

使用 yt-dlp 下载 B 站视频的音频流。
B 站音频原生为 m4a 格式，faster-whisper 通过内置 PyAV 可直接处理，
无需安装 ffmpeg 即可完成转录。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
import logging
import re

import yt_dlp

from .config import config
from .fetcher import VideoInfo

logger = logging.getLogger(__name__)

# B 站音频流的原生格式（DASH m4a），faster-whisper 的 PyAV 可直接读取
_NATIVE_AUDIO_EXT = "m4a"

# 检测 ffmpeg 是否可用（仅用于日志提示，不影响核心流程）
_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


@dataclass
class DownloadResult:
    bvid: str
    audio_path: Path | None
    success: bool
    error: str = ""


class AudioDownloader:
    """将 B 站视频的音频流下载到本地（无需 ffmpeg）"""

    def __init__(self):
        config.audio_dir.mkdir(parents=True, exist_ok=True)
        if not _FFMPEG_AVAILABLE:
            logger.info(
                "未检测到 ffmpeg，将直接保存 B 站原生 m4a 音频（faster-whisper 可直接处理）"
            )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def download(self, video: VideoInfo) -> DownloadResult:
        """下载单个视频的音频，已存在则跳过。"""
        # 先找已存在的任意格式文件（m4a / mp3 均可）
        existing = self._find_audio_file(video.bvid)
        if existing:
            logger.info("音频已存在，跳过下载: %s", existing.name)
            return DownloadResult(bvid=video.bvid, audio_path=existing, success=True)

        logger.info("开始下载音频: [%s] %s", video.bvid, video.title)

        ydl_opts = self._build_opts(video.bvid)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video.url])
        except yt_dlp.utils.DownloadError as e:
            logger.error("下载失败 [%s]: %s", video.bvid, e)
            return DownloadResult(bvid=video.bvid, audio_path=None, success=False, error=str(e))
        except Exception as e:
            logger.error("下载异常 [%s]: %s", video.bvid, e)
            return DownloadResult(bvid=video.bvid, audio_path=None, success=False, error=str(e))

        found = self._find_audio_file(video.bvid)
        if found:
            logger.info("下载完成: %s (%.1f MB)", found.name, found.stat().st_size / 1e6)
            return DownloadResult(bvid=video.bvid, audio_path=found, success=True)

        msg = f"下载后未找到音频文件（bvid={video.bvid}，目录={config.audio_dir}）"
        logger.error(msg)
        return DownloadResult(bvid=video.bvid, audio_path=None, success=False, error=msg)

    def download_batch(self, videos: list[VideoInfo]) -> list[DownloadResult]:
        results = []
        for i, video in enumerate(videos, 1):
            logger.info("下载进度 %d/%d", i, len(videos))
            results.append(self.download(video))
        return results

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _find_audio_file(self, bvid: str) -> Path | None:
        """在 audio_dir 中搜索包含 bvid 的音频文件（任意格式）"""
        pattern = re.compile(re.escape(bvid), re.IGNORECASE)
        for f in config.audio_dir.iterdir():
            if f.is_file() and f.suffix.lstrip(".") in ("m4a", "mp3", "wav", "flac", "ogg", "opus") \
                    and pattern.search(f.name):
                return f
        return None

    def _build_opts(self, bvid: str) -> dict:
        """构造 yt-dlp 选项字典。

        核心策略：
        - 若 ffmpeg 可用：下载 bestaudio 并转为配置的格式（mp3 等）
        - 若 ffmpeg 不可用：直接保存 B 站原生 m4a，faster-whisper 通过 PyAV 直接读取
        """
        output_template = str(config.audio_dir / f"{bvid}.%(ext)s")

        opts: dict = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "quiet": False,
            "no_warnings": False,
            "logger": _YtDlpLogger(),
            "retries": config.download_max_retries,
            "http_headers": {
                "Referer": "https://www.bilibili.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        }

        if _FFMPEG_AVAILABLE:
            # ffmpeg 可用时转换格式
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": config.audio_format,
                    "preferredquality": str(config.audio_quality),
                }
            ]
        # ffmpeg 不可用时不加 postprocessors，保留原生 m4a

        # cookies 对 yt-dlp 同样生效（解锁登录态，避免 412 风控）
        cookie_file = self._resolve_cookie_file()
        if cookie_file:
            opts["cookiefile"] = cookie_file

        return opts

    @staticmethod
    def _resolve_cookie_file() -> str:
        """
        解析 yt-dlp 可用的 cookies 文件路径：
          1. 优先使用 download.cookies_file（用户显式配置的 Netscape 文件）
          2. 否则用 bilibili.sessdata / bili_jct / dedeuserid 自动生成一个
             临时 Netscape cookies 文件（缓存在 data/ 下，避免每次重建）
        """
        if config.cookies_file:
            return config.cookies_file

        sessdata   = config.get("bilibili", "sessdata",   default="")
        bili_jct   = config.get("bilibili", "bili_jct",   default="")
        dedeuserid = config.get("bilibili", "dedeuserid", default="")
        if not sessdata:
            return ""

        from urllib.parse import unquote
        # SESSDATA 配置里是 URL 编码形式，写文件前解码为原始值
        sessdata = unquote(sessdata)
        lines = [
            "# Netscape HTTP Cookie File",
            "# 由 config.yaml 的 bilibili.sessdata 自动生成，请勿手动编辑",
            ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tSESSDATA\t" + sessdata,
        ]
        if bili_jct:
            lines.append(".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tbili_jct\t" + bili_jct)
        if dedeuserid:
            lines.append(".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tDedeUserID\t" + dedeuserid)

        cookie_path = config.audio_dir.parent / "_bili_cookies.txt"
        try:
            cookie_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            logger.info("已根据 bilibili.sessdata 生成 yt-dlp cookies 文件: %s", cookie_path)
            return str(cookie_path)
        except Exception as e:
            logger.warning("生成 cookies 文件失败: %s", e)
            return ""


class _YtDlpLogger:
    """将 yt-dlp 日志桥接到 Python logging 系统"""

    def debug(self, msg: str):
        if msg.startswith("[debug]"):
            logger.debug(msg)
        else:
            logger.info(msg)

    def info(self, msg: str):
        logger.info(msg)

    def warning(self, msg: str):
        logger.warning(msg)

    def error(self, msg: str):
        logger.error(msg)
