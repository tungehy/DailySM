"""
语音转文字模块

使用 faster-whisper 将音频文件转录为中文文本。
faster-whisper 相比 openai-whisper 速度快 2-4 倍，内存占用更低。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
import logging
import json

from .config import config

logger = logging.getLogger(__name__)

# 将本地 faster-whisper 代码加入 Python 路径
_FASTER_WHISPER_DIR = Path(__file__).parent.parent / "faster-whisper-master"
if _FASTER_WHISPER_DIR.exists() and str(_FASTER_WHISPER_DIR) not in sys.path:
    sys.path.insert(0, str(_FASTER_WHISPER_DIR))


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    bvid: str
    audio_path: Path
    transcript_path: Path | None    # 保存的 .txt 文件路径
    full_text: str                  # 拼接后的完整文本
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str = "zh"
    success: bool = True
    error: str = ""


class WhisperTranscriber:
    """使用 faster-whisper 转录音频"""

    _model = None        # 延迟加载，避免启动就占用大量内存
    _force_cpu = False   # CUDA 推理失败后置 True，强制后续用 CPU

    def __init__(self):
        config.transcript_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def transcribe(self, bvid: str, audio_path: Path) -> TranscriptResult:
        """
        转录单个音频文件。
        若已有转录缓存则直接读取，跳过重复计算。
        """
        transcript_path = config.transcript_dir / f"{bvid}.txt"

        if transcript_path.exists():
            logger.info("转录缓存命中，直接读取: %s", transcript_path.name)
            text = transcript_path.read_text(encoding="utf-8")
            return TranscriptResult(
                bvid=bvid,
                audio_path=audio_path,
                transcript_path=transcript_path,
                full_text=text,
                success=True,
            )

        logger.info("开始转录: %s", audio_path.name)

        # 最多尝试两次：第一次用配置的 device，若是 CUDA 错误则自动切 CPU 重试
        for attempt in range(2):
            try:
                model = self._get_model()
                result = self._run_transcribe(model, bvid, audio_path, transcript_path)
                return result
            except RuntimeError as e:
                err_str = str(e).lower()
                is_cuda_err = any(k in err_str for k in ("cublas", "cudnn", "cuda", "cufft", "curand"))
                if is_cuda_err and attempt == 0:
                    logger.warning(
                        "CUDA 推理失败（%s），重置模型并切换到 CPU int8 模式重试…", e
                    )
                    # 销毁坏的 CUDA 模型，下次 _get_model() 会重建为 CPU 版本
                    WhisperTranscriber._model = None
                    WhisperTranscriber._force_cpu = True
                    continue
                logger.error("转录失败 [%s]: %s", bvid, e, exc_info=True)
                return TranscriptResult(
                    bvid=bvid, audio_path=audio_path, transcript_path=None,
                    full_text="", success=False, error=str(e),
                )
            except Exception as e:
                logger.error("转录失败 [%s]: %s", bvid, e, exc_info=True)
                return TranscriptResult(
                    bvid=bvid, audio_path=audio_path, transcript_path=None,
                    full_text="", success=False, error=str(e),
                )

        # 不应到达此处
        return TranscriptResult(
            bvid=bvid, audio_path=audio_path, transcript_path=None,
            full_text="", success=False, error="未知错误",
        )

    def _run_transcribe(
        self, model, bvid: str, audio_path: Path, transcript_path: Path
    ) -> "TranscriptResult":
        """实际执行转录，抛出异常由调用方处理。"""
        segments_gen, info = model.transcribe(
            str(audio_path),
            language=config.whisper_language or None,
            beam_size=5,
            vad_filter=config.whisper_vad_filter,
            vad_parameters={"min_silence_duration_ms": 500},
        )

        segments: list[TranscriptSegment] = []
        for seg in segments_gen:
            segments.append(
                TranscriptSegment(start=seg.start, end=seg.end, text=seg.text.strip())
            )

        detected_lang = info.language
        full_text = "\n".join(s.text for s in segments if s.text)

        logger.info(
            "转录完成: %s，语言=%s，片段数=%d，字符数=%d",
            bvid, detected_lang, len(segments), len(full_text),
        )

        transcript_path.write_text(full_text, encoding="utf-8")
        detail_path = config.transcript_dir / f"{bvid}_detail.json"
        self._save_detail(detail_path, bvid, segments, detected_lang)

        return TranscriptResult(
            bvid=bvid,
            audio_path=audio_path,
            transcript_path=transcript_path,
            full_text=full_text,
            segments=segments,
            language=detected_lang,
            success=True,
        )

    def transcribe_batch(
        self, tasks: list[tuple[str, Path]]
    ) -> list[TranscriptResult]:
        """
        批量转录。
        tasks: [(bvid, audio_path), ...]
        """
        results = []
        for i, (bvid, audio_path) in enumerate(tasks, 1):
            logger.info("转录进度 %d/%d", i, len(tasks))
            result = self.transcribe(bvid, audio_path)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get_model(self):
        """延迟加载 WhisperModel，仅初始化一次。CUDA 不可用时自动降级到 CPU。"""
        if WhisperTranscriber._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise ImportError(
                    "无法导入 faster_whisper，请确认：\n"
                    "  1. 已安装 faster-whisper: pip install faster-whisper\n"
                    f"  2. 或本地源码路径正确: {_FASTER_WHISPER_DIR}"
                )

            # _force_cpu 由推理阶段的 CUDA 错误触发
            if WhisperTranscriber._force_cpu:
                device, compute_type = "cpu", "int8"
                logger.info("（CPU 降级模式）加载 Whisper 模型: size=%s, device=cpu, compute_type=int8",
                            config.whisper_model_size)
            else:
                device = config.whisper_device
                compute_type = config.whisper_compute_type
                logger.info("加载 Whisper 模型: size=%s, device=%s, compute_type=%s",
                            config.whisper_model_size, device, compute_type)

            try:
                WhisperTranscriber._model = WhisperModel(
                    config.whisper_model_size,
                    device=device,
                    compute_type=compute_type,
                )
            except Exception as e:
                if device != "cpu":
                    logger.warning(
                        "GPU 模型加载失败（%s），自动切换到 CPU int8 模式。\n"
                        "如需 GPU 加速，请安装 CUDA 12 + cuDNN 9：\n"
                        "  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12",
                        e,
                    )
                    WhisperTranscriber._model = WhisperModel(
                        config.whisper_model_size, device="cpu", compute_type="int8",
                    )
                else:
                    raise

            logger.info("Whisper 模型加载完成")
        return WhisperTranscriber._model

    @staticmethod
    def _save_detail(
        path: Path,
        bvid: str,
        segments: list[TranscriptSegment],
        language: str,
    ):
        data = {
            "bvid": bvid,
            "language": language,
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text}
                for s in segments
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
