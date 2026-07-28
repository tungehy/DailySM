"""
研报 RAG 知识库 Skill（时效加权检索）

核心理念——Time-Decayed RAG：
  投资知识会过期，但不同内容过期速度不同。检索排序不只看语义相似度，
  还乘以「时间衰减系数」，让相关但过时的研报自动降权。

      最终得分 = 语义相似度 × 时间衰减
      时间衰减 = 0.5 ^ (已过天数 / 半衰期)

  半衰期由 report_type 决定（宏观策略慢、数据快评快）。

分层：
  research_reports  研报原文 + 元数据（Raw 层）
  research_chunks   切块 + 向量（JSONB）+ 冗余时效字段（Vector 层）
        │  search() 时效加权检索
        ▼
  由 ResearchAgent 抽取观点 → market_opinions（Opinion 层）

向量存储：
  当前用 JSONB 存向量 + Python 余弦相似度（零系统依赖，适合手动导入低量场景）。
  接口设计与 pgvector 一致，未来 PG 装了 vector 扩展可平滑切换。
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Column, DateTime, Index, Integer, MetaData, String, Table, Text,
    delete, func, select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)
metadata = MetaData()

# ---------------------------------------------------------------------------
# 时效配置：report_type → 半衰期（天）
# ---------------------------------------------------------------------------

HALF_LIFE_DAYS: dict[str, float] = {
    "macro_strategy": 180,   # 宏观/年度策略：大逻辑变化慢
    "industry_deep":  90,    # 行业深度：产业中周期
    "company_note":   30,    # 公司/事件点评：时效较短
    "data_flash":     7,     # 数据/快评：快速失效
    "default":        60,    # 未标注类型的兜底
}

# 切块参数默认值
_DEFAULT_CHUNK_SIZE    = 500
_DEFAULT_CHUNK_OVERLAP = 80


def load_research_cfg() -> dict:
    """从 config.yaml research 段读取配置（半衰期覆盖、切块、检索参数）"""
    try:
        from pathlib import Path
        import yaml
        cfg_path = Path(__file__).parent.parent / "config" / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("research", {}) or {}
    except Exception:
        return {}


def _half_life(report_type: str) -> float:
    cfg = load_research_cfg().get("half_life_days", {}) or {}
    merged = {**HALF_LIFE_DAYS, **cfg}
    return float(merged.get(report_type or "default", merged.get("default", 60)))


# ---------------------------------------------------------------------------
# 表定义
# ---------------------------------------------------------------------------

research_reports = Table(
    "research_reports",
    metadata,
    Column("id",           Integer,     primary_key=True, autoincrement=True),
    Column("title",        Text,        nullable=False),
    Column("institution",  String(96),  nullable=True),
    Column("analyst",      String(96),  nullable=True),
    Column("report_type",  String(32),  nullable=False, default=""),
    Column("publish_time", DateTime,    nullable=False),
    Column("industries",   JSONB,       nullable=True),   # list[str]
    Column("summary",      Text,        nullable=True),
    Column("raw_text",     Text,        nullable=False),
    Column("source_file",  Text,        nullable=True),
    Column("file_path",    Text,        nullable=True),   # 持久化的原始文件路径（PDF原件等）
    Column("doc_hash",     String(64),  nullable=False, unique=True),
    Column("chunk_count",  Integer,     nullable=False, default=0),
    Column("created_at",   DateTime,    nullable=False),
)

research_chunks = Table(
    "research_chunks",
    metadata,
    Column("id",           Integer,     primary_key=True, autoincrement=True),
    Column("report_id",    Integer,     nullable=False),
    Column("chunk_index",  Integer,     nullable=False),
    Column("content",      Text,        nullable=False),
    Column("embedding",    JSONB,       nullable=True),    # list[float]
    # 冗余时效字段，避免检索时 JOIN
    Column("publish_time", DateTime,    nullable=False),
    Column("report_type",  String(32),  nullable=False, default=""),
    Column("industries",   JSONB,       nullable=True),
    Column("created_at",   DateTime,    nullable=False),
)

Index("ix_rr_publish",   research_reports.c.publish_time)
Index("ix_rr_type",      research_reports.c.report_type)
Index("ix_rc_report_id", research_chunks.c.report_id)
Index("ix_rc_publish",   research_chunks.c.publish_time)


def init_research_tables(engine) -> None:
    """建表（幂等）+ 轻量迁移（补充 file_path 列）"""
    metadata.create_all(engine)
    _migrate(engine)
    logger.info("[research_db] research_reports + research_chunks 表初始化完成")


def _migrate(engine) -> None:
    """为已存在的 research_reports 补充新增列（幂等）"""
    from sqlalchemy import text
    ddl = "ALTER TABLE research_reports ADD COLUMN IF NOT EXISTS file_path TEXT"
    try:
        with engine.begin() as conn:
            conn.execute(text(ddl))
    except Exception as e:
        logger.debug("[research_db] 迁移 file_path 列: %s", e)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def compute_doc_hash(title: str, raw_text: str) -> str:
    """基于标题 + 正文前2000字生成去重 hash"""
    raw = f"{title}::{raw_text[:2000]}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:64]


def chunk_text(
    text: str,
    size:    int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """
    按段落聚合切块，尽量不切断句子。
    先按空行/换行分段，累积到接近 size 就成块，块间保留 overlap 字符重叠。
    """
    if not text:
        return []
    # 先按段落
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            # 段落本身超长则硬切
            if len(para) > size:
                for i in range(0, len(para), size - overlap):
                    chunks.append(para[i:i + size])
                buf = ""
            else:
                # 带 overlap 起头
                tail = buf[-overlap:] if buf and overlap else ""
                buf = f"{tail}\n{para}" if tail else para
    if buf:
        chunks.append(buf)
    return chunks


def time_decay(
    publish_time: datetime,
    report_type:  str,
    as_of:        datetime | None = None,
) -> float:
    """0.5 ^ (已过天数 / 半衰期)，范围 (0, 1]"""
    now = as_of or datetime.now()
    if not isinstance(publish_time, datetime):
        return 1.0
    days = max(0.0, (now - publish_time).total_seconds() / 86400.0)
    hl = _half_life(report_type)
    if hl <= 0:
        return 1.0
    return math.pow(0.5, days / hl)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def insert_report(engine, embed_client, report: dict) -> dict:
    """
    导入一篇研报：切块 → 向量化 → 写 research_reports + research_chunks。

    report 必填：title, raw_text
    可选：institution, analyst, report_type, publish_time(str/date/datetime),
          industries(list), summary, source_file

    doc_hash 冲突则跳过（幂等）。返回 {"report_id", "chunks", "skipped"}。
    """
    title    = (report.get("title") or "").strip() or "无标题研报"
    raw_text = report.get("raw_text") or ""
    if not raw_text.strip():
        raise ValueError("raw_text 为空，无法导入")

    now         = datetime.now()
    doc_hash    = compute_doc_hash(title, raw_text)
    report_type = report.get("report_type") or ""
    publish_dt  = _to_datetime(report.get("publish_time"), fallback=now)
    industries  = report.get("industries") or []

    # 去重检查
    with engine.connect() as conn:
        existing = conn.execute(
            select(research_reports.c.id).where(research_reports.c.doc_hash == doc_hash)
        ).fetchone()
    if existing:
        logger.info("[research_db] 研报已存在，跳过：%s（id=%d）", title[:40], existing[0])
        return {"report_id": existing[0], "chunks": 0, "skipped": True}

    # 切块
    cfg   = load_research_cfg()
    size  = int(cfg.get("chunk_size", _DEFAULT_CHUNK_SIZE))
    ovlp  = int(cfg.get("chunk_overlap", _DEFAULT_CHUNK_OVERLAP))
    parts = chunk_text(raw_text, size=size, overlap=ovlp)
    if not parts:
        raise ValueError("切块结果为空")

    # 向量化
    logger.info("[research_db] 《%s》切成 %d 块，开始向量化…", title[:40], len(parts))
    vectors = embed_client.embed(parts)

    # 写 report
    with engine.begin() as conn:
        r = conn.execute(
            research_reports.insert().values(
                title        = title,
                institution  = report.get("institution"),
                analyst      = report.get("analyst"),
                report_type  = report_type,
                publish_time = publish_dt,
                industries   = industries,
                summary      = report.get("summary"),
                raw_text     = raw_text,
                source_file  = report.get("source_file"),
                file_path    = report.get("file_path"),
                doc_hash     = doc_hash,
                chunk_count  = len(parts),
                created_at   = now,
            )
        )
        report_id = r.inserted_primary_key[0]

        # 写 chunks
        chunk_rows = []
        for i, (content, vec) in enumerate(zip(parts, vectors)):
            chunk_rows.append({
                "report_id":    report_id,
                "chunk_index":  i,
                "content":      content,
                "embedding":    vec or None,
                "publish_time": publish_dt,
                "report_type":  report_type,
                "industries":   industries,
                "created_at":   now,
            })
        conn.execute(research_chunks.insert(), chunk_rows)

    logger.info("[research_db] 导入完成：《%s》id=%d，%d 块", title[:40], report_id, len(parts))
    return {"report_id": report_id, "chunks": len(parts), "skipped": False}


# ---------------------------------------------------------------------------
# 检索（时效加权 RAG）
# ---------------------------------------------------------------------------

def search(
    engine,
    embed_client,
    query:          str,
    top_k:          int = 6,
    industry:       str | None = None,
    as_of:          datetime | None = None,
    drop_threshold: float = 0.15,
    candidate_limit: int = 500,
) -> list[dict]:
    """
    时效加权语义检索。

    步骤：
      1. query 向量化
      2. 载入候选 chunk（可按行业过滤）
      3. 逐块计算 语义相似度 × 时间衰减
      4. 丢弃时间衰减 < drop_threshold 的过期块
      5. 返回 top_k

    每条结果附加：similarity, decay, score, 及所属研报元数据。
    """
    now = as_of or datetime.now()
    q_vec = embed_client.embed_one(query)
    if not q_vec:
        logger.warning("[research_db] query 向量化失败")
        return []

    # 载入候选
    stmt = select(research_chunks)
    if industry:
        # JSONB 包含该行业
        stmt = stmt.where(research_chunks.c.industries.contains([industry]))
    stmt = stmt.order_by(research_chunks.c.publish_time.desc()).limit(candidate_limit)

    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings().all()]

    if not rows:
        return []

    from skills.embedding_client import EmbeddingClient
    scored: list[dict] = []
    for r in rows:
        vec = r.get("embedding")
        if not vec:
            continue
        sim   = EmbeddingClient.cosine_similarity(q_vec, vec)
        decay = time_decay(r["publish_time"], r.get("report_type", ""), now)
        if decay < drop_threshold:
            continue   # 过期，丢弃
        r["similarity"] = round(sim, 4)
        r["decay"]      = round(decay, 4)
        r["score"]      = round(sim * decay, 4)
        scored.append(r)

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_k]

    # 附加研报元数据
    report_ids = list({r["report_id"] for r in top})
    meta = _load_report_meta(engine, report_ids)
    for r in top:
        m = meta.get(r["report_id"], {})
        r["report_title"] = m.get("title")
        r["institution"]  = m.get("institution")
        r["analyst"]      = m.get("analyst")
    return top


def _load_report_meta(engine, report_ids: list[int]) -> dict[int, dict]:
    if not report_ids:
        return {}
    stmt = select(
        research_reports.c.id, research_reports.c.title,
        research_reports.c.institution, research_reports.c.analyst,
    ).where(research_reports.c.id.in_(report_ids))
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return {r["id"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# 管理
# ---------------------------------------------------------------------------

def list_reports(engine, limit: int = 100) -> list[dict]:
    """列出已导入研报（按发布时间倒序）"""
    stmt = select(
        research_reports.c.id, research_reports.c.title,
        research_reports.c.institution, research_reports.c.analyst,
        research_reports.c.report_type, research_reports.c.publish_time,
        research_reports.c.industries, research_reports.c.chunk_count,
        research_reports.c.created_at, research_reports.c.file_path,
    ).order_by(research_reports.c.publish_time.desc()).limit(limit)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def delete_report(engine, report_id: int) -> int:
    """删除研报及其所有 chunk"""
    with engine.begin() as conn:
        conn.execute(delete(research_chunks).where(research_chunks.c.report_id == report_id))
        r = conn.execute(delete(research_reports).where(research_reports.c.id == report_id))
    return r.rowcount


def get_known_industries(engine, as_of: datetime | None = None,
                         min_decay: float = 0.15) -> list[str]:
    """
    返回当前仍有「新鲜」研报覆盖的行业列表（供 ResearchAgent 自动确定关注面）。
    """
    now = as_of or datetime.now()
    stmt = select(
        research_reports.c.industries, research_reports.c.publish_time,
        research_reports.c.report_type,
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    fresh: set[str] = set()
    for r in rows:
        if time_decay(r["publish_time"], r.get("report_type", ""), now) < min_decay:
            continue
        for ind in (r.get("industries") or []):
            if ind:
                fresh.add(ind)
    return sorted(fresh)


def count_reports(engine) -> dict[str, int]:
    """统计研报与 chunk 数量"""
    with engine.connect() as conn:
        n_reports = conn.execute(select(func.count()).select_from(research_reports)).scalar()
        n_chunks  = conn.execute(select(func.count()).select_from(research_chunks)).scalar()
    return {"reports": n_reports or 0, "chunks": n_chunks or 0}


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _to_datetime(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(value[:len(fmt) + 2], fmt)
            except ValueError:
                continue
    return fallback
