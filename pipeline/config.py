"""
配置读取模块
"""
from pathlib import Path
from typing import Any
import yaml
import logging

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent


class Config:
    """全局配置单例，读取 config.yaml"""

    _instance: "Config | None" = None
    _data: dict = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def load(self, config_path: str | Path | None = None) -> None:
        path = Path(config_path) if config_path else _PROJECT_ROOT / "config" / "config.yaml"
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}
        self._loaded = True
        logger.info("已加载配置文件: %s", path)

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def get(self, *keys: str, default: Any = None) -> Any:
        """按点路径访问配置，例如 get('bilibili', 'days_filter')"""
        self._ensure_loaded()
        node = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # ---- 便捷属性 ----

    @property
    def up_hosts(self) -> list[dict]:
        return self.get("bilibili", "up_hosts", default=[])

    @property
    def days_filter(self) -> int:
        return self.get("bilibili", "days_filter", default=1)

    @property
    def request_interval(self) -> float:
        return self.get("bilibili", "request_interval", default=2)

    @property
    def audio_dir(self) -> Path:
        rel = self.get("download", "audio_dir", default="data/audio")
        return _PROJECT_ROOT / rel

    @property
    def audio_format(self) -> str:
        return self.get("download", "audio_format", default="mp3")

    @property
    def audio_quality(self) -> int:
        return self.get("download", "audio_quality", default=0)

    @property
    def download_max_retries(self) -> int:
        return self.get("download", "max_retries", default=3)

    @property
    def cookies_file(self) -> str:
        val = self.get("download", "cookies_file", default="")
        if val:
            p = _PROJECT_ROOT / val
            return str(p) if p.exists() else ""
        return ""

    @property
    def whisper_model_size(self) -> str:
        return self.get("transcription", "model_size", default="medium")

    @property
    def whisper_device(self) -> str:
        return self.get("transcription", "device", default="cpu")

    @property
    def whisper_compute_type(self) -> str:
        return self.get("transcription", "compute_type", default="int8")

    @property
    def whisper_language(self) -> str:
        return self.get("transcription", "language", default="zh")

    @property
    def whisper_vad_filter(self) -> bool:
        return self.get("transcription", "vad_filter", default=True)

    @property
    def transcript_dir(self) -> Path:
        rel = self.get("transcription", "transcript_dir", default="data/transcripts")
        return _PROJECT_ROOT / rel

    # 支持的 provider 及其默认 base_url / chat path
    # 同时兼容旧名称（volc_engine）和新名称（ark）
    _PROVIDER_DEFAULTS: dict[str, dict] = {
        "openai":      {"base_url": "https://api.openai.com/v1",                      "chat_path": "/chat/completions"},
        "deepseek":    {"base_url": "https://api.deepseek.com/v1",                    "chat_path": "/chat/completions"},
        "qianwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "chat_path": "/chat/completions"},
        "zhipu":       {"base_url": "https://open.bigmodel.cn/api/paas/v4",           "chat_path": "/chat/completions"},
        "volc_engine": {"base_url": "https://ark.cn-beijing.volces.com/api/v3",       "chat_path": "/chat/completions"},
        "ark":         {"base_url": "https://ark.cn-beijing.volces.com/api/v3",       "chat_path": "/chat/completions"},
    }

    @property
    def llm_provider(self) -> str:
        return self.get("llm", "provider", default="ark")

    @property
    def llm_api_key(self) -> str:
        """
        读取 LLM API Key。
        支持两种配置格式：
          新格式（嵌套）: llm.{provider}.api_key
          旧格式（平铺）: llm.api_key
        """
        provider = self.llm_provider
        val = self.get("llm", provider, "api_key", default="")
        if val:
            return val
        return self.get("llm", "api_key", default="")

    @property
    def llm_base_url(self) -> str:
        """返回 base_url：优先读嵌套配置，其次平铺，最后按 provider 给默认值"""
        provider = self.llm_provider
        val = self.get("llm", provider, "base_url", default="")
        if val:
            return val
        val = self.get("llm", "base_url", default="")
        if val:
            return val
        defaults = self._PROVIDER_DEFAULTS.get(provider, {})
        return defaults.get("base_url", "https://api.openai.com/v1")

    @property
    def llm_chat_path(self) -> str:
        """返回 chat completions 的路径段，各厂商版本号不同"""
        provider = self.llm_provider
        defaults = self._PROVIDER_DEFAULTS.get(provider, {})
        return defaults.get("chat_path", "/chat/completions")

    @property
    def llm_model(self) -> str:
        """支持嵌套格式（llm.{provider}.model）和平铺格式（llm.model）"""
        provider = self.llm_provider
        val = self.get("llm", provider, "model", default="")
        if val:
            return val
        return self.get("llm", "model", default="gpt-4o-mini")

    @property
    def llm_max_tokens(self) -> int:
        return self.get("llm", "max_tokens", default=4096)

    @property
    def llm_temperature(self) -> float:
        return self.get("llm", "temperature", default=0.3)

    @property
    def llm_timeout(self) -> int:
        return self.get("llm", "timeout", default=60)

    @property
    def report_dir(self) -> Path:
        rel = self.get("report", "output_dir", default="data/reports")
        return _PROJECT_ROOT / rel

    @property
    def report_title_prefix(self) -> str:
        return self.get("report", "title_prefix", default="A股板块行情日报")

    @property
    def generate_html(self) -> bool:
        return self.get("report", "generate_html", default=False)

    @property
    def db_path(self) -> Path:
        rel = self.get("database", "path", default="data/cache/state.db")
        return _PROJECT_ROOT / rel

    @property
    def log_level(self) -> str:
        return self.get("logging", "level", default="INFO")

    @property
    def log_file(self) -> Path | None:
        val = self.get("logging", "log_file", default="")
        return _PROJECT_ROOT / val if val else None


config = Config()
