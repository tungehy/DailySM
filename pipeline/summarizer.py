"""
LLM 总结模块

将多条转录文本整合，调用 LLM API 生成每日 A 股板块行情分析报告。
支持 OpenAI 兼容接口（火山引擎方舟 / DeepSeek / 通义千问 / 智谱 / OpenAI 等）。

火山引擎方舟（Ark）接入说明：
  - 控制台：https://console.volcengine.com/ark
  - API Key 页面：控制台 → API Key 管理
  - 模型名称：doubao-1-5-pro-32k / doubao-pro-32k / doubao-lite-32k 等
  - 推理接入点 ID（可选）：ep-XXXXXX-XXXXX（在「推理接入点」页创建后使用）
  - Base URL：https://ark.cn-beijing.volces.com/api/v3
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
import textwrap

import httpx

from .config import config
from .fetcher import VideoInfo
from .transcriber import TranscriptResult

logger = logging.getLogger(__name__)

# ---- Prompt 模板 --------------------------------------------------------

_SYSTEM_PROMPT = textwrap.dedent("""\
    你是一位专业的 A 股市场板块行情分析师助手。
    你的任务是根据股票分析视频的语音转录文稿，提炼出今日各板块行情的关键信息，
    生成一份结构清晰、重点突出的市场日报。

    要求：
    1. 语言：简体中文，专业但易懂
    2. 格式：严格按照指定 Markdown 结构输出
    3. 内容：只提炼视频中明确提到的信息，不要臆造数据
    4. 若多个 UP 主对同一板块有不同观点，应并列呈现并注明来源
    5. 对板块的涨跌判断使用"↑强势 / ↓弱势 / →震荡"等符号标注
""")

_USER_PROMPT_TEMPLATE = textwrap.dedent("""\
    今天是 {date}。

    以下是今日 B 站股票 UP 主的视频转录文稿，请据此生成每日板块行情总结报告。

    {transcripts_section}

    ---

    请严格按照以下 Markdown 格式输出报告，不要添加任何格式之外的说明文字：

    # {title_prefix} {date}

    ## 📈 市场总体概述
    （用 2-3 句话概括今日大盘整体走势和市场情绪）

    ## 🔥 强势板块
    （列出今日表现强势的板块，每条格式：**板块名** ↑强势 — 核心逻辑/驱动因素）

    ## 📉 弱势板块
    （列出今日表现弱势的板块，格式同上）

    ## 💡 重点关注
    （UP 主重点提示的个股或主题机会，每条格式：**标的名** — 关键信息 [来源: UP 主昵称]）

    ## ⚠️ 风险提示
    （UP 主提到的市场风险点，简洁列出）

    ## 📝 原始观点摘录
    （按 UP 主分段，摘录其核心判断，保留原话风格）

    ### {up_names_placeholder}

    ---
    *本报告由 AI 根据视频转录生成，仅供参考，不构成投资建议。*
""")


@dataclass
class VideoTranscriptPair:
    video: VideoInfo
    transcript: TranscriptResult


@dataclass
class SummaryResult:
    report_date: date
    markdown_content: str
    success: bool
    error: str = ""
    token_usage: dict | None = None


class MarketSummarizer:
    """调用 LLM 生成板块行情日报"""

    def __init__(self):
        self._validate_config()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def summarize(
        self,
        pairs: list[VideoTranscriptPair],
        report_date: date | None = None,
    ) -> SummaryResult:
        """
        传入视频+转录配对列表，调用 LLM 生成报告。

        Args:
            pairs:       VideoTranscriptPair 列表
            report_date: 报告日期，默认 today

        Returns:
            SummaryResult
        """
        if report_date is None:
            report_date = date.today()

        logger.info("开始生成 %s 行情报告，共 %d 条视频", report_date, len(pairs))

        # 构建转录内容 section
        transcripts_section = self._build_transcripts_section(pairs)
        up_names = "、".join(
            {p.video.up_name for p in pairs if p.transcript.success}
        )

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            date=report_date.strftime("%Y年%m月%d日"),
            transcripts_section=transcripts_section,
            title_prefix=config.report_title_prefix,
            up_names_placeholder=up_names or "UP 主",
        )

        try:
            content, usage = self._call_llm(_SYSTEM_PROMPT, user_prompt)
            logger.info("LLM 报告生成完成，字符数=%d", len(content))
            return SummaryResult(
                report_date=report_date,
                markdown_content=content,
                success=True,
                token_usage=usage,
            )
        except Exception as e:
            logger.error("LLM 调用失败: %s", e, exc_info=True)
            return SummaryResult(
                report_date=report_date,
                markdown_content="",
                success=False,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _build_transcripts_section(pairs: list[VideoTranscriptPair]) -> str:
        """将多条转录文本格式化为 Prompt 中的内容块"""
        lines: list[str] = []
        for pair in pairs:
            if not pair.transcript.success or not pair.transcript.full_text.strip():
                logger.warning("视频 %s 转录为空或失败，跳过", pair.video.bvid)
                continue

            # 截断超长文本（避免超出 context 限制，约 15k 字）
            text = pair.transcript.full_text
            if len(text) > 15000:
                text = text[:15000] + "\n…（内容过长已截断）"

            lines.append(
                f"### 来源: {pair.video.up_name} — 《{pair.video.title}》\n"
                f"（发布时间: {pair.video.pub_date.strftime('%Y-%m-%d %H:%M')}，"
                f"时长: {pair.video.duration // 60} 分钟）\n\n"
                f"{text}\n"
            )

        if not lines:
            return "（今日暂无有效转录内容）"
        return "\n---\n".join(lines)

    def _call_llm(self, system_prompt: str, user_prompt: str) -> tuple[str, dict | None]:
        """
        调用 OpenAI 兼容 API（支持火山引擎方舟及其他厂商）。
        返回 (content_text, usage_dict)。
        """
        url = self._build_url()
        payload = self._build_payload(system_prompt, user_prompt)
        headers = {
            "Authorization": f"Bearer {config.llm_api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(
            "请求 LLM API: url=%s, provider=%s, model=%s",
            url, config.llm_provider, config.llm_model,
        )

        with httpx.Client(timeout=config.llm_timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                # 附上响应体以便排查鉴权/配额错误
                body = e.response.text[:500]
                raise RuntimeError(
                    f"HTTP {e.response.status_code} — {body}"
                ) from e

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage")

        if usage:
            logger.debug(
                "Token 用量: prompt=%s, completion=%s, total=%s",
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
            )

        return content, usage

    def _build_payload(self, system_prompt: str, user_prompt: str) -> dict:
        """构造请求 payload，火山引擎支持全部标准字段"""
        payload: dict = {
            "model": config.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": config.llm_max_tokens,
            "temperature": config.llm_temperature,
        }

        # 火山引擎方舟：可附加 thinking 参数（仅 doubao-1-5-thinking 系列需要）
        # 普通 doubao 模型不需要，保持默认即可
        if config.llm_provider in ("volc_engine", "ark"):
            # stream 默认关闭，如需流式可在此开启
            payload["stream"] = False

        return payload

    def _build_url(self) -> str:
        """拼接完整的 chat completions 请求 URL"""
        base = config.llm_base_url.rstrip("/")
        path = config.llm_chat_path   # 各厂商路径不同，由 config 统一管理
        return f"{base}{path}"

    def _validate_config(self):
        provider = config.llm_provider
        api_key = config.llm_api_key

        placeholder_keys = {"YOUR_API_KEY_HERE", "YOUR_ARK_API_KEY_HERE", ""}
        if not api_key or api_key in placeholder_keys:
            logger.warning(
                "LLM API Key 未配置，请在 config.yaml 的 llm.api_key 中填写"
            )

        if provider == "volc_engine":
            logger.info(
                "使用火山引擎方舟（Ark）接口，模型: %s，endpoint: %s",
                config.llm_model,
                config.llm_base_url,
            )
            if not config.llm_model:
                logger.warning(
                    "未指定模型名称，请在 llm.model 填写，例如: doubao-1-5-pro-32k"
                )
