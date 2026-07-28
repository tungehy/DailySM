"""
统一 LLM 客户端

支持两种 Provider（在 config.yaml 中切换）：
  ark    — 火山引擎 Ark（OpenAI 兼容，当前默认）
  openai — 任意 OpenAI 兼容接口（DeepSeek / Qwen / 本地模型等）

使用 openai SDK（两种 provider 均兼容）。

注意：对于带推理链的模型（如 DeepSeek-R1），chat() 会保留推理过程以供展示，
      而 chat_json() 使用独立通道 _fetch_content_only()，只取 msg.content，
      避免推理内容中的 {...} 干扰 JSON 提取。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml
from openai import OpenAI

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_PATH  = _PROJECT_ROOT / "config" / "config.yaml"


def _load_llm_cfg() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("llm", {})


class LLMClient:
    """
    统一 LLM 调用客户端。

    通过 config.yaml [llm] 段自动初始化，支持：
      - provider: ark       → 火山引擎 Ark（已配置）
      - provider: openai    → 任意 OpenAI 兼容（DeepSeek 等）

    主要方法：
        chat(messages)          → str  （保留推理过程，供展示）
        chat_json(messages)     → dict（仅使用 content，带 JSON 解析 + 重试）
        sector_prompt(prompt)   → str  （快捷方法，自动加板块分析 system prompt）
    """

    def __init__(self, cfg: dict | None = None):
        cfg      = cfg or _load_llm_cfg()
        provider = cfg.get("provider", "ark")

        # 从对应 provider 子节点读取接入点配置；
        # 兼容旧格式（api_key/base_url/model 直接平铺在 cfg 顶层）。
        sub = cfg.get(provider, {}) or {}

        api_key  = sub.get("api_key")  or cfg.get("api_key",  "")
        base_url = sub.get("base_url") or cfg.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
        model    = sub.get("model")    or cfg.get("model",    "")

        self._client      = OpenAI(api_key=api_key, base_url=base_url)
        self._model       = model
        self._max_tokens  = int(cfg.get("max_tokens",  4096))
        self._temperature = float(cfg.get("temperature", 0.3))
        self._timeout     = int(cfg.get("timeout",     120))

        logger.debug("LLMClient 初始化：provider=%s  model=%s", provider, self._model)

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        include_reasoning: bool = True,
    ) -> str:
        """
        调用 LLM，返回文字响应。

        include_reasoning=True（默认）：推理模型会在正文前附带【推理过程】。
        include_reasoning=False：只返回最终答案（对话场景推荐，避免展示思考过程）。
        """
        try:
            resp = self._client.chat.completions.create(
                model       = self._model,
                messages    = messages,
                temperature = temperature if temperature is not None else self._temperature,
                max_tokens  = max_tokens  if max_tokens  is not None else self._max_tokens,
                timeout     = self._timeout,
            )
            msg    = resp.choices[0].message
            result = ""
            if include_reasoning and hasattr(msg, "reasoning_content") and msg.reasoning_content:
                result += f"【推理过程】\n{msg.reasoning_content}\n\n"
            result += (msg.content or "")
            return result.strip() or "（空响应）"
        except Exception as e:
            logger.error("LLM 调用失败: %s", e)
            return f"[LLM ERROR] {e}"

    def chat_json(
        self,
        messages:   list[dict[str, str]],
        max_tokens: int | None = None,
        retries:    int = 2,
    ) -> dict[str, Any] | list:
        """
        调用 LLM 并解析 JSON 响应。

        关键：只使用 msg.content，不包含 reasoning_content，
        避免推理内容中的 {...} 干扰正则提取。

        若解析失败则自动重试（最多 retries 次），仍失败则返回 {}。
        """
        for attempt in range(retries + 1):
            content = self._fetch_content_only(messages, max_tokens=max_tokens)
            parsed  = self._extract_json(content)
            if parsed is not None:
                return parsed
            if attempt < retries:
                logger.warning("JSON 解析失败（第 %d 次），重试…", attempt + 1)
                logger.debug("原始 content（前400字）: %s", content[:400])
                time.sleep(1)
        logger.error("LLM JSON 解析始终失败，返回空字典")
        return {}

    def sector_prompt(self, user_prompt: str, system: str = "") -> str:
        """板块分析专用快捷方法（自动附加行业分析 system 角色）"""
        system = system or (
            "你是专业的A股行业板块分析师，擅长从宏观、资金、舆情等多维度分析行业板块投资机会，"
            "专注板块日线级别低频交易分析。"
        )
        return self.chat([
            {"role": "system", "content": system},
            {"role": "user",   "content": user_prompt},
        ])

    # ------------------------------------------------------------------
    # 内部：仅获取 content（不含推理链）
    # ------------------------------------------------------------------

    def _fetch_content_only(
        self,
        messages:    list[dict[str, str]],
        max_tokens:  int | None = None,
        temperature: float | None = None,
    ) -> str:
        """
        内部方法：只返回 msg.content，用于 chat_json。
        推理模型的 reasoning_content 会被丢弃，防止干扰 JSON 提取。
        """
        try:
            resp = self._client.chat.completions.create(
                model       = self._model,
                messages    = messages,
                temperature = temperature if temperature is not None else 0.2,
                max_tokens  = max_tokens  if max_tokens  is not None else self._max_tokens,
                timeout     = self._timeout,
            )
            content = (resp.choices[0].message.content or "").strip()
            # 若 content 为空但有 reasoning_content（极少数模型配置），降级兜底
            if not content:
                rc = getattr(resp.choices[0].message, "reasoning_content", "") or ""
                if rc:
                    logger.debug("content 为空，尝试从 reasoning_content 提取 JSON")
                    content = rc.strip()
            return content
        except Exception as e:
            logger.error("LLM 调用失败: %s", e)
            return ""

    # ------------------------------------------------------------------
    # 工具：JSON 提取
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> dict | list | None:
        """
        从 LLM 纯文本 content 中提取合法 JSON 对象或数组。

        策略（优先级从高到低）：
          1. 直接 json.loads（模型输出纯 JSON 时最快）
          2. 提取 ```json ... ``` 代码块
          3. 从文本末尾反向扫描，找最后一个完整 JSON 块
             （推理模型偶尔在 content 前段也输出分析文字）
          4. 正向找第一个完整 JSON 块（兜底）
        """
        if not text:
            return None

        text = text.strip()

        # 1. 直接解析
        try:
            return json.loads(text)
        except Exception:
            pass

        # 2. ```json / ``` 代码块
        for m in re.finditer(r"```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```", text):
            try:
                return json.loads(m.group(1))
            except Exception:
                continue

        # 3. 从末尾往前找最后一个完整 JSON 块（贪婪匹配最外层括号）
        #    先找 { 和 [ 的所有起始位置，从后往前尝试
        candidates: list[str] = []
        for bracket_open, bracket_close in [('{', '}'), ('[', ']')]:
            # 找最后一个 close bracket，逐步向前找配对的 open bracket
            last = text.rfind(bracket_close)
            if last == -1:
                continue
            depth = 0
            for i in range(last, -1, -1):
                if text[i] == bracket_close:
                    depth += 1
                elif text[i] == bracket_open:
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[i:last + 1])
                        break

        for cand in candidates:
            try:
                return json.loads(cand)
            except Exception:
                continue

        # 4. 正向找第一个完整块（兜底）
        for m in re.finditer(r"([\[{][\s\S]*?[\]}])", text):
            try:
                return json.loads(m.group(1))
            except Exception:
                continue

        return None
