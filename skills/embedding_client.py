"""
Embedding 客户端（OpenAI 兼容接口）

用途：
  - 新闻语义去重（相似度阈值过滤）
  - 历史新闻检索（向量相似度搜索）
  - 行业语义匹配（辅助 NewsAgent 行业映射）
  - 未来：结合向量数据库做 RAG

配置（config.yaml embedding 段）：
  embedding:
    provider: ark          # ark / openai
    ark:
      api_key:  YOUR_KEY
      base_url: https://ark.cn-beijing.volces.com/api/v3
      model:    YOUR_EMBEDDING_MODEL_ID
      dimensions: 2048     # 可选，按模型实际维度填写
    openai:
      api_key:  YOUR_KEY
      base_url: https://api.openai.com/v1
      model:    text-embedding-3-small
      dimensions: 1536
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_PATH  = _PROJECT_ROOT / "config" / "config.yaml"

# 单次批量请求最大文本数
_MAX_BATCH = 64


def _load_embed_cfg() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("embedding", {})


class EmbeddingClient:
    """
    OpenAI 兼容 Embedding 客户端。

    主要方法：
        embed(texts)                    → list[list[float]]
        embed_one(text)                 → list[float]
        cosine_similarity(v1, v2)       → float  （-1 ~ 1）
        most_similar(query, candidates) → list[(index, score)]
        deduplicate(texts, threshold)   → list[str]  （语义去重）
    """

    def __init__(self, cfg: dict | None = None):
        cfg      = cfg or _load_embed_cfg()
        provider = cfg.get("provider", "ark")

        from openai import OpenAI

        if provider == "openai":
            sub = cfg.get("openai", {})
        else:
            sub = cfg.get("ark", {})

        self._client     = OpenAI(
            api_key  = sub.get("api_key", "sk-placeholder"),
            base_url = sub.get("base_url", "https://api.openai.com/v1"),
        )
        self._model      = sub.get("model", "text-embedding-3-small")
        self._dimensions = sub.get("dimensions")   # None 表示不传（使用模型默认）
        logger.debug("EmbeddingClient 初始化：provider=%s  model=%s", provider, self._model)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        批量获取文本向量。

        Args:
            texts: 文本列表，单次最多 _MAX_BATCH 条（超过自动分批）

        Returns:
            list[list[float]]，与 texts 等长，对应每条文本的向量
        """
        if not texts:
            return []

        all_vectors: list[list[float]] = []
        for i in range(0, len(texts), _MAX_BATCH):
            batch   = [_truncate(t) for t in texts[i:i + _MAX_BATCH]]
            vectors = self._embed_batch(batch)
            all_vectors.extend(vectors)
        return all_vectors

    def embed_one(self, text: str) -> list[float]:
        """获取单条文本向量"""
        results = self.embed([text])
        return results[0] if results else []

    @staticmethod
    def cosine_similarity(v1: list[float], v2: list[float]) -> float:
        """计算两向量余弦相似度（-1 ~ 1）"""
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        dot   = sum(a * b for a, b in zip(v1, v2))
        norm1 = math.sqrt(sum(a * a for a in v1))
        norm2 = math.sqrt(sum(b * b for b in v2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)

    def most_similar(
        self,
        query:      str,
        candidates: list[str],
        top_k:      int = 5,
    ) -> list[tuple[int, float]]:
        """
        找与 query 最相似的候选文本。

        Returns:
            [(index_in_candidates, score), ...] 按相似度降序，最多 top_k 条
        """
        if not candidates:
            return []
        all_texts   = [query] + candidates
        all_vectors = self.embed(all_texts)
        if not all_vectors:
            return []
        q_vec      = all_vectors[0]
        cand_vecs  = all_vectors[1:]
        scored     = [
            (i, self.cosine_similarity(q_vec, v))
            for i, v in enumerate(cand_vecs)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def deduplicate(
        self,
        texts:     list[str],
        threshold: float = 0.92,
    ) -> list[str]:
        """
        语义去重：移除与已保留文本相似度 >= threshold 的条目。

        Args:
            texts:     待去重文本列表（按优先级排序，靠前的被保留）
            threshold: 相似度阈值（0~1，越高越严格）

        Returns:
            去重后的文本列表
        """
        if not texts:
            return []
        vectors = self.embed(texts)
        if not vectors:
            return texts   # 若 embedding 失败，原样返回

        kept: list[int] = []
        for i, v_i in enumerate(vectors):
            is_dup = False
            for j in kept:
                if self.cosine_similarity(v_i, vectors[j]) >= threshold:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(i)

        logger.debug(
            "EmbeddingClient.deduplicate: %d → %d（阈值=%.2f）",
            len(texts), len(kept), threshold,
        )
        return [texts[i] for i in kept]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调用 API，获取一批文本的向量"""
        try:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": texts,
            }
            if self._dimensions:
                kwargs["dimensions"] = self._dimensions
            resp = self._client.embeddings.create(**kwargs)
            # 按 index 排序保证顺序
            data = sorted(resp.data, key=lambda x: x.index)
            return [d.embedding for d in data]
        except Exception as e:
            logger.error("Embedding API 调用失败: %s", e)
            return [[] for _ in texts]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int = 2000) -> str:
    """截断过长文本，避免超出 embedding 模型 token 限制"""
    return text[:max_chars] if text else ""


# ---------------------------------------------------------------------------
# 单例（可选）
# ---------------------------------------------------------------------------

_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    """获取全局单例 EmbeddingClient（懒加载）"""
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client
