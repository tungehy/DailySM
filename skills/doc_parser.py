"""
多格式研报文档解析 Skill

支持格式：
  .docx        Word 文档（python-docx）
  .pdf         纯文字 PDF（pypdf，扫描件无法提取）
  .md          Markdown（支持 YAML frontmatter 元数据）
  .json        结构化 JSON（直接读取字段）
  .txt         纯文本

统一输出 dict：
  {
    "title":        标题,
    "raw_text":     正文全文,
    "institution":  机构（可选）,
    "analyst":      分析师（可选）,
    "report_type":  报告类型（可选，见 research_db.HALF_LIFE_DAYS）,
    "publish_time": 发布日期 str "YYYY-MM-DD"（可选）,
    "industries":   涉及行业 list[str]（可选）,
    "source_file":  源文件路径,
  }

元数据优先级：文件内声明（frontmatter/json 字段） > 调用方显式传参 > 缺省。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 允许的报告类型（与 research_db.HALF_LIFE_DAYS 对齐）
_KNOWN_TYPES = {"macro_strategy", "industry_deep", "company_note", "data_flash"}

# 元数据字段规范化映射（中英文 → 标准键）
_META_ALIASES: dict[str, str] = {
    "title": "title", "标题": "title",
    "institution": "institution", "机构": "institution", "券商": "institution",
    "analyst": "analyst", "分析师": "analyst", "作者": "analyst",
    "report_type": "report_type", "type": "report_type", "类型": "report_type", "报告类型": "report_type",
    "publish_time": "publish_time", "date": "publish_time", "发布日期": "publish_time", "日期": "publish_time",
    "industries": "industries", "industry": "industries", "行业": "industries", "板块": "industries",
    "summary": "summary", "摘要": "summary",
}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def parse_file(path: str | Path) -> dict[str, Any]:
    """
    解析单个研报文件，返回标准化 dict。
    未识别的元数据字段忽略；正文为空时抛 ValueError。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")

    suffix = p.suffix.lower()
    if suffix == ".docx":
        result = _parse_docx(p)
    elif suffix == ".pdf":
        result = _parse_pdf(p)
    elif suffix in (".md", ".markdown"):
        result = _parse_markdown(p)
    elif suffix == ".json":
        result = _parse_json(p)
    elif suffix in (".txt", ".text"):
        result = _parse_txt(p)
    else:
        raise ValueError(f"不支持的文件格式: {suffix}（支持 docx/pdf/md/json/txt）")

    result.setdefault("title", p.stem)
    result["source_file"] = str(p)
    result = _normalize(result)

    if not (result.get("raw_text") or "").strip():
        raise ValueError(f"未能从文件提取到正文: {p}")
    return result


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------

def _parse_docx(p: Path) -> dict:
    import docx  # python-docx
    doc = docx.Document(str(p))
    paras = [para.text for para in doc.paragraphs if para.text and para.text.strip()]
    # 表格文本也提取
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                paras.append(" | ".join(cells))
    text = "\n".join(paras)
    title = paras[0][:120] if paras else p.stem
    return {"title": title, "raw_text": text}


def _parse_pdf(p: Path) -> dict:
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    pages = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            pages.append(t)
    text = "\n".join(pages)
    # 尝试从 PDF 元信息取标题
    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)
    except Exception:
        pass
    if not title:
        first_line = text.strip().splitlines()[0] if text.strip() else p.stem
        title = first_line[:120]
    return {"title": title, "raw_text": text}


def _parse_markdown(p: Path) -> dict:
    raw = p.read_text(encoding="utf-8")
    meta: dict = {}
    body = raw

    # YAML frontmatter：--- ... ---
    if raw.lstrip().startswith("---"):
        try:
            import yaml
            stripped = raw.lstrip()
            end = stripped.find("\n---", 3)
            if end != -1:
                fm = stripped[3:end].strip()
                meta = yaml.safe_load(fm) or {}
                body = stripped[end + 4:].lstrip("\n")
        except Exception as e:
            logger.debug("[doc_parser] frontmatter 解析失败: %s", e)

    result = dict(meta) if isinstance(meta, dict) else {}
    result["raw_text"] = body
    if "title" not in result:
        # 用首个 # 标题
        for line in body.splitlines():
            if line.strip().startswith("#"):
                result["title"] = line.lstrip("#").strip()[:120]
                break
    return result


def _parse_json(p: Path) -> dict:
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON 研报应为对象（dict），支持字段见 doc_parser 文档")
    # 正文字段兼容多种命名
    text = (
        data.get("raw_text") or data.get("content") or data.get("text")
        or data.get("正文") or ""
    )
    result = dict(data)
    result["raw_text"] = text
    return result


def _parse_txt(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    first_line = text.strip().splitlines()[0] if text.strip() else p.stem
    return {"title": first_line[:120], "raw_text": text}


# ---------------------------------------------------------------------------
# 元数据规范化
# ---------------------------------------------------------------------------

def _normalize(result: dict) -> dict:
    """把别名字段映射到标准键，规范化 industries / publish_time / report_type"""
    normalized: dict[str, Any] = {}
    for k, v in result.items():
        key = _META_ALIASES.get(str(k).strip().lower(), None) or _META_ALIASES.get(str(k).strip(), k)
        normalized[key] = v

    # industries → list[str]
    ind = normalized.get("industries")
    if isinstance(ind, str):
        normalized["industries"] = [x.strip() for x in ind.replace("，", ",").split(",") if x.strip()]
    elif ind is None:
        normalized["industries"] = []

    # report_type 校验
    rt = str(normalized.get("report_type", "") or "").strip()
    normalized["report_type"] = rt if rt in _KNOWN_TYPES else (rt or "")

    # publish_time → "YYYY-MM-DD"
    normalized["publish_time"] = _normalize_date(normalized.get("publish_time"))

    return normalized


def _normalize_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10] if len(s) >= 8 else None
