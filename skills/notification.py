"""
通知中心（Notification Center）

设计原则：
  - 所有通知渠道统一由 NotificationCenter 管理。
  - 业务代码（Agent、Scheduler）只调用 NotificationCenter，
    不得直接调用 DingTalk / Feishu / Webhook。
  - 后续新增通知渠道，只需在此文件添加 Channel 实现和 config.yaml 配置，
    无需修改任何 Agent。

支持渠道（均基于 Webhook）：
  DingTalkChannel  — 钉钉自定义机器人（支持加签）
  FeishuChannel    — 飞书自定义机器人
  WeComChannel     — 企业微信群机器人

通知类型：
  daily_report     — 每日决策日报（最重要）
  agent_complete   — 单个 Agent 分析完成（可选，默认关闭，防止过于频繁）
  agent_error      — Agent 分析失败
  scheduler_error  — 调度器任务失败

使用方式：
    nc = NotificationCenter()           # 从 config.yaml 读取配置
    nc.notify_daily_report(result)      # 推送决策日报
    nc.notify_error("HeatAgent", "...")  # 推送错误告警

配置样例（config.yaml）：
    notifications:
      enabled: true
      channels:
        dingtalk:
          enabled: true
          webhook: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
          secret: ""   # 加签秘钥（可选）
        feishu:
          enabled: false
          webhook: ""
        wecom:
          enabled: false
          webhook: ""
      on_daily_report: true
      on_agent_complete: false
      on_agent_error: true
      on_scheduler_error: true
"""
from __future__ import annotations

import hashlib
import hmac
import json
import base64
import logging
import time
import urllib.parse
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT   = Path(__file__).parent.parent
_CONFIG_PATH    = _PROJECT_ROOT / "config" / "config.yaml"


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _load_notifications_cfg() -> dict:
    """从 config.yaml 读取 notifications 配置块"""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("notifications", {})
    except Exception as e:
        logger.debug("[NotificationCenter] 配置加载失败: %s", e)
        return {}


# ---------------------------------------------------------------------------
# 消息等级
# ---------------------------------------------------------------------------

class MsgLevel:
    INFO    = "info"
    WARNING = "warning"
    ERROR   = "error"
    SUCCESS = "success"


# ---------------------------------------------------------------------------
# 抽象基类 Channel
# ---------------------------------------------------------------------------

class BaseChannel(ABC):
    """所有通知渠道的抽象基类"""

    name: str = "base"

    def __init__(self, cfg: dict):
        self.cfg     = cfg
        self.enabled = cfg.get("enabled", False)
        self.webhook = cfg.get("webhook", "").strip()

    def is_usable(self) -> bool:
        return self.enabled and bool(self.webhook)

    @abstractmethod
    def send(self, title: str, content: str, level: str = MsgLevel.INFO) -> bool:
        """
        发送通知。
        成功返回 True，失败记录警告并返回 False（不抛出异常）。
        """
        ...

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> bool:
        """通用 HTTP POST（同步，无额外依赖）"""
        import urllib.request
        data    = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req     = urllib.request.Request(
            url,
            data    = data,
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                **(headers or {}),
            },
            method  = "POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                resp_json = json.loads(body)
                return self._check_response(resp_json)
        except Exception as e:
            logger.warning("[%s] HTTP 请求失败: %s", self.name, e)
            return False

    def _check_response(self, resp: dict) -> bool:
        """子类可覆盖此方法检查响应体"""
        return True


# ---------------------------------------------------------------------------
# 钉钉（DingTalk）
# ---------------------------------------------------------------------------

class DingTalkChannel(BaseChannel):
    """
    钉钉自定义机器人（Webhook）

    支持加签（secret）防盗用：
      https://open.dingtalk.com/document/robots/customize-robot-security-settings

    消息格式：Markdown（钉钉支持有限 Markdown）
    """

    name = "dingtalk"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.secret = cfg.get("secret", "").strip()

    def _build_url(self) -> str:
        """若配置了 secret，生成带签名的 URL"""
        if not self.secret:
            return self.webhook
        timestamp = str(round(time.time() * 1000))
        content   = f"{timestamp}\n{self.secret}"
        sign      = base64.b64encode(
            hmac.new(
                self.secret.encode("utf-8"),
                content.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        sign_enc  = urllib.parse.quote_plus(sign)
        return f"{self.webhook}&timestamp={timestamp}&sign={sign_enc}"

    def send(self, title: str, content: str, level: str = MsgLevel.INFO) -> bool:
        if not self.is_usable():
            return False

        # 钉钉 Markdown 格式消息
        # title 会显示在消息列表预览和 @ 通知标题中
        text = _level_emoji(level) + f" **{title}**\n\n{content}"

        payload = {
            "msgtype":  "markdown",
            "markdown": {
                "title": title,
                "text":  text,
            },
        }
        ok = self._post(self._build_url(), payload)
        if ok:
            logger.debug("[DingTalk] 发送成功: %s", title)
        else:
            logger.warning("[DingTalk] 发送失败: %s", title)
        return ok

    def _check_response(self, resp: dict) -> bool:
        errcode = resp.get("errcode", -1)
        if errcode == 0:
            return True
        logger.warning("[DingTalk] 接口返回错误: %s", resp)
        return False


# ---------------------------------------------------------------------------
# 飞书（Feishu）
# ---------------------------------------------------------------------------

class FeishuChannel(BaseChannel):
    """
    飞书自定义机器人（Webhook）

    消息格式：富文本 post（支持换行、粗体、链接）
    文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
    """

    name = "feishu"

    def send(self, title: str, content: str, level: str = MsgLevel.INFO) -> bool:
        if not self.is_usable():
            return False

        emoji   = _level_emoji(level)
        # 飞书富文本：content 按行分割，每行作为一个 text element
        lines   = content.split("\n")
        content_blocks = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                content_blocks.append([{"tag": "text", "text": ""}])
                continue
            # 粗体（以 ** 包裹的文字转为 bold）
            if stripped.startswith("**") and "**" in stripped[2:]:
                content_blocks.append([{"tag": "text", "text": stripped.replace("**", ""), "style": ["bold"]}])
            else:
                content_blocks.append([{"tag": "text", "text": stripped}])

        payload = {
            "msg_type": "post",
            "content":  {
                "post": {
                    "zh_cn": {
                        "title":   f"{emoji} {title}",
                        "content": content_blocks,
                    }
                }
            },
        }
        ok = self._post(self.webhook, payload)
        if ok:
            logger.debug("[Feishu] 发送成功: %s", title)
        else:
            logger.warning("[Feishu] 发送失败: %s", title)
        return ok

    def _check_response(self, resp: dict) -> bool:
        code = resp.get("code", -1)
        if code == 0:
            return True
        logger.warning("[Feishu] 接口返回错误: %s", resp)
        return False


# ---------------------------------------------------------------------------
# 企业微信（WeCom）
# ---------------------------------------------------------------------------

class WeComChannel(BaseChannel):
    """
    企业微信群机器人（Webhook）

    消息格式：Markdown（企业微信支持标准 Markdown 子集）
    文档：https://developer.work.weixin.qq.com/document/path/91770
    """

    name = "wecom"

    def send(self, title: str, content: str, level: str = MsgLevel.INFO) -> bool:
        if not self.is_usable():
            return False

        emoji   = _level_emoji(level)
        md_text = f"## {emoji} {title}\n{content}"
        # 企业微信 markdown 单条消息最大 4096 字节
        if len(md_text.encode("utf-8")) > 4000:
            md_text = md_text.encode("utf-8")[:4000].decode("utf-8", errors="ignore") + "…"

        payload = {
            "msgtype":  "markdown",
            "markdown": {"content": md_text},
        }
        ok = self._post(self.webhook, payload)
        if ok:
            logger.debug("[WeCom] 发送成功: %s", title)
        else:
            logger.warning("[WeCom] 发送失败: %s", title)
        return ok

    def _check_response(self, resp: dict) -> bool:
        errcode = resp.get("errcode", -1)
        if errcode == 0:
            return True
        logger.warning("[WeCom] 接口返回错误: %s", resp)
        return False


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

_LEVEL_EMOJI = {
    MsgLevel.INFO:    "ℹ️",
    MsgLevel.WARNING: "⚠️",
    MsgLevel.ERROR:   "🚨",
    MsgLevel.SUCCESS: "✅",
}

def _level_emoji(level: str) -> str:
    return _LEVEL_EMOJI.get(level, "")


_CHANNEL_CLASSES: dict[str, type[BaseChannel]] = {
    "dingtalk": DingTalkChannel,
    "feishu":   FeishuChannel,
    "wecom":    WeComChannel,
}


# ---------------------------------------------------------------------------
# 消息格式化
# ---------------------------------------------------------------------------

def _format_daily_report(result) -> tuple[str, str]:
    """
    将 AgentResult（decision）格式化为通知消息。
    返回 (title, content)。
    """
    trade_date = getattr(result, "trade_date", date.today())
    top_sectors = getattr(result, "top_sectors", [])
    confidence  = getattr(result, "confidence", 0.0)

    # 标题
    title = f"行业板块 AI 决策日报 | {trade_date}"

    # 看多 / 看空板块
    bullish = [s for s in top_sectors if s.get("direction") == "bullish"][:5]
    bearish = [s for s in top_sectors if s.get("direction") == "bearish"][:3]

    bull_text = "\n".join(
        f"  - **{s['sector']}** ({s.get('score', 0):.0f}分)  {s.get('reason', '')[:30]}"
        for s in bullish
    ) or "  暂无明确看多板块"

    bear_text = "\n".join(
        f"  - {s['sector']} ({s.get('score', 0):.0f}分)  {s.get('reason', '')[:30]}"
        for s in bearish
    ) or "  无"

    # Agent 来源
    sub_results = result.raw_data.get("agent_results", []) if hasattr(result, "raw_data") else []
    sources = "、".join(r.get("agent_name", "") for r in sub_results[:5]) or getattr(result, "mode", "")

    content = (
        f"> 综合置信度：{confidence:.0%}   数据来源：{sources}\n\n"
        f"**看多板块（Top 5）**\n{bull_text}\n\n"
        f"**看空板块**\n{bear_text}\n\n"
        f"*{datetime.now().strftime('%H:%M')} 生成 · 仅供参考，注意风险*"
    )
    return title, content


def _format_agent_complete(result) -> tuple[str, str]:
    """单个 Agent 完成通知（简洁）"""
    agent_name  = getattr(result, "agent_name", "?")
    trade_date  = getattr(result, "trade_date", date.today())
    confidence  = getattr(result, "confidence", 0.0)
    top_sectors = getattr(result, "top_sectors", [])

    title = f"[{agent_name.upper()}] 分析完成 | {trade_date}"
    top3  = "、".join(s.get("sector", "") for s in top_sectors[:3]) or "（无）"
    content = (
        f"置信度：{confidence:.0%}\n"
        f"推荐板块（Top 3）：{top3}"
    )
    return title, content


def _format_error(source: str, message: str) -> tuple[str, str]:
    """错误告警通知"""
    title   = f"[告警] {source} 运行失败"
    content = (
        f"**来源**: {source}\n\n"
        f"**错误**: {message[:500]}\n\n"
        f"*{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*"
    )
    return title, content


# ---------------------------------------------------------------------------
# NotificationCenter
# ---------------------------------------------------------------------------

class NotificationCenter:
    """
    通知中心 — 统一管理所有推送渠道。

    业务代码只需调用本类的高层方法，无需关心具体渠道实现。

    实例化时从 config.yaml 的 notifications 配置块读取渠道配置。
    若 notifications.enabled = false 或无 webhook，则静默忽略所有通知（不报错）。
    """

    def __init__(self, cfg: dict | None = None):
        """
        Args:
            cfg: 直接传入配置字典（测试用）；为 None 时自动从 config.yaml 加载。
        """
        if cfg is None:
            cfg = _load_notifications_cfg()
        self._cfg      = cfg
        self._enabled  = cfg.get("enabled", False)
        self._channels = self._build_channels(cfg)
        self._on       = {
            "daily_report":    cfg.get("on_daily_report",    True),
            "agent_complete":  cfg.get("on_agent_complete",  False),
            "agent_error":     cfg.get("on_agent_error",     True),
            "scheduler_error": cfg.get("on_scheduler_error", True),
        }
        active = [ch.name for ch in self._channels if ch.is_usable()]
        if active:
            logger.info("[NotificationCenter] 已加载渠道: %s", active)
        elif self._enabled:
            logger.warning("[NotificationCenter] enabled=true 但无可用渠道（请检查 webhook 配置）")

    # ------------------------------------------------------------------
    # 高层业务接口（业务代码只调用这些方法）
    # ------------------------------------------------------------------

    def notify_daily_report(self, result) -> None:
        """
        推送每日决策日报。
        result: DecisionAgent 的 AgentResult。
        """
        if not self._on.get("daily_report"):
            return
        title, content = _format_daily_report(result)
        self._broadcast(title, content, MsgLevel.SUCCESS)

    def notify_agent_complete(self, result) -> None:
        """
        推送单个 Agent 完成通知（config.yaml on_agent_complete 默认关闭）。
        """
        if not self._on.get("agent_complete"):
            return
        title, content = _format_agent_complete(result)
        self._broadcast(title, content, MsgLevel.INFO)

    def notify_error(self, source: str, message: str) -> None:
        """
        推送错误告警（Agent 失败、调度器异常等）。
        source: 错误来源，如 "HeatAgent" / "scheduler:nbs_cpi_ppi"
        """
        # 区分 Agent 错误和调度器错误
        event = "scheduler_error" if "scheduler" in source.lower() else "agent_error"
        if not self._on.get(event):
            return
        title, content = _format_error(source, message)
        self._broadcast(title, content, MsgLevel.ERROR)

    def notify(
        self,
        title:   str,
        content: str,
        level:   str = MsgLevel.INFO,
    ) -> None:
        """
        通用通知接口（供不适合上面三个专用方法的场景使用）。
        level: MsgLevel.INFO / WARNING / ERROR / SUCCESS
        """
        self._broadcast(title, content, level)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _broadcast(self, title: str, content: str, level: str) -> None:
        """向所有可用渠道广播消息"""
        if not self._enabled:
            return
        usable = [ch for ch in self._channels if ch.is_usable()]
        if not usable:
            return
        for ch in usable:
            try:
                ch.send(title, content, level)
            except Exception as e:
                logger.warning("[NotificationCenter] 渠道 %s 发送异常: %s", ch.name, e)

    @staticmethod
    def _build_channels(cfg: dict) -> list[BaseChannel]:
        channels: list[BaseChannel] = []
        for ch_name, cls in _CHANNEL_CLASSES.items():
            ch_cfg = cfg.get("channels", {}).get(ch_name, {})
            try:
                channels.append(cls(ch_cfg))
            except Exception as e:
                logger.warning("[NotificationCenter] 初始化渠道 %s 失败: %s", ch_name, e)
        return channels

    # ------------------------------------------------------------------
    # 测试工具
    # ------------------------------------------------------------------

    def test_all_channels(self) -> dict[str, bool]:
        """
        向所有已启用渠道发送测试消息。
        返回 {channel_name: success} 字典。
        供命令行测试使用：
          python -c "from skills.notification import NotificationCenter; NotificationCenter().test_all_channels()"
        """
        results = {}
        for ch in self._channels:
            if not ch.is_usable():
                results[ch.name] = None  # None = 未配置/未启用
                continue
            ok = ch.send(
                title   = "行业板块 AI 平台 — 通知测试",
                content = (
                    f"渠道 **{ch.name}** 配置验证通过\n\n"
                    f"*{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*"
                ),
                level   = MsgLevel.INFO,
            )
            results[ch.name] = ok
            logger.info("[NotificationCenter] 测试 %s: %s", ch.name, "成功" if ok else "失败")
        return results

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active_channels(self) -> list[str]:
        return [ch.name for ch in self._channels if ch.is_usable()]


# ---------------------------------------------------------------------------
# 全局单例（懒加载）
# ---------------------------------------------------------------------------

_instance: NotificationCenter | None = None


def get_notification_center() -> NotificationCenter:
    """
    获取 NotificationCenter 全局单例（懒加载）。
    推荐使用此函数而不是直接实例化，可避免多次读取配置。
    """
    global _instance
    if _instance is None:
        _instance = NotificationCenter()
    return _instance


def reset_notification_center() -> None:
    """重置单例（主要用于测试）"""
    global _instance
    _instance = None
