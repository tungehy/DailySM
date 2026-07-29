"""Knowledge（知识中心）：研报管理、RAG 检索、研报阅读"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ..deps import get_engine, to_jsonable

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# 内存中的导入处理状态：report_id 或临时 key -> status
_import_status: dict[str, dict] = {}
_import_lock = threading.Lock()


@router.get("/reports")
def list_reports(limit: int = 100, date_from: str | None = None, date_to: str | None = None):
    """研报列表（含处理状态），支持按发布日期区间筛选 date_from/date_to=YYYY-MM-DD"""
    from skills.research_db import list_reports

    engine = get_engine()
    rows = list_reports(engine, limit=limit)
    if date_from or date_to:
        def _in_range(r):
            pt = r.get("publish_time")
            pt = (pt.strftime("%Y-%m-%d") if hasattr(pt, "strftime") else str(pt or ""))[:10]
            if not pt:
                return True   # 无日期的处理中记录始终保留
            if date_from and pt < date_from:
                return False
            if date_to and pt > date_to:
                return False
            return True
        rows = [r for r in rows if _in_range(r)]
    for r in rows:
        # 内存里若有处理状态，优先标注（刚上传未入库的不在此列表）
        rid = str(r.get("id"))
        if rid in _import_status:
            r["status"] = _import_status[rid].get("status", "done")
        else:
            r["status"] = "done"   # 已入库即完成（embedding 同步写入）
    with _import_lock:
        # 补充正在处理（尚未入库）的记录
        for key, st in _import_status.items():
            if st.get("pending") and st.get("status") != "done":
                rows.append({
                    "id":         key,
                    "title":      st.get("title", "（处理中）"),
                    "institution": st.get("institution", ""),
                    "report_type": st.get("report_type", ""),
                    "publish_time": None,
                    "industries": [],
                    "chunk_count": 0,
                    "created_at": None,
                    "status":     st.get("status", "processing"),
                })
    return {"reports": to_jsonable(rows)}


@router.get("/reports/{report_id}")
def report_detail(report_id: str):
    """研报详情：标题/元信息 + 正文 + AI 摘要"""
    from sqlalchemy import select
    from skills.research_db import research_reports

    engine = get_engine()
    try:
        rid = int(report_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="研报不存在或处理中")
    stmt = select(research_reports).where(research_reports.c.id == rid)
    with engine.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="研报不存在")
    d = dict(row)
    file_path = d.get("file_path") or ""
    return {
        "id":          d["id"],
        "title":       d.get("title"),
        "institution": d.get("institution"),
        "analyst":     d.get("analyst"),
        "report_type": d.get("report_type"),
        "publish_time": str(d.get("publish_time") or ""),
        "industries":  to_jsonable(d.get("industries")),
        "summary":     d.get("summary"),
        "raw_text":    d.get("raw_text"),
        "chunk_count": d.get("chunk_count"),
        "created_at":  str(d.get("created_at", "")),
        "file_ext":    Path(file_path).suffix.lower().lstrip(".") if file_path else None,
        "has_file":    bool(file_path),
    }


_MIME = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
         ".json": "application/json", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


@router.get("/reports/{report_id}/file")
def report_file(report_id: str):
    """返回研报原始文件（PDF 原件等，供前端 iframe 直接渲染）"""
    from sqlalchemy import select
    from skills.research_db import research_reports

    engine = get_engine()
    try:
        rid = int(report_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="研报不存在")
    stmt = select(research_reports.c.file_path, research_reports.c.title).where(
        research_reports.c.id == rid)
    with engine.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    if not row or not row.get("file_path"):
        raise HTTPException(status_code=404, detail="该研报没有原始文件")
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="原始文件已被删除")
    mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
    # inline 让浏览器在 iframe 中直接渲染原件；中文名用 RFC5987 filename* 编码，避免 attachment 触发下载
    from urllib.parse import quote
    fname = quote(path.name)
    return FileResponse(
        path, media_type=mime,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{fname}"},
    )


@router.delete("/reports/{report_id}")
def delete_report(report_id: str):
    """删除研报（含向量切块）"""
    from skills.research_db import delete_report as _del

    engine = get_engine()
    try:
        rid = int(report_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法研报 id")
    n = _del(engine, rid)
    if n == 0:
        raise HTTPException(status_code=404, detail="研报不存在")
    return {"deleted": n}


from pydantic import BaseModel


class BatchDeleteIn(BaseModel):
    ids: list[int]


@router.post("/reports/batch-delete")
def batch_delete_reports(body: BatchDeleteIn):
    """批量删除研报（含向量切块）"""
    from skills.research_db import delete_report as _del

    engine = get_engine()
    deleted = 0
    for rid in body.ids:
        try:
            deleted += _del(engine, int(rid))
        except Exception:
            continue
    # 清除对应内存状态
    with _import_lock:
        for rid in body.ids:
            _import_status.pop(str(rid), None)
    return {"deleted": deleted, "requested": len(body.ids)}


ALLOWED_EXT = {".md", ".txt", ".json", ".docx", ".pdf"}

# 原始研报文件存储目录（PDF 原件等，供前端 iframe 直接渲染）
from ..deps import PROJECT_ROOT
RESEARCH_FILES_DIR = PROJECT_ROOT / "data" / "research_files"
RESEARCH_FILES_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/reports/upload")
async def upload_reports(files: list[UploadFile] = File(...)):
    """批量导入研报：保存原始文件 → 后台线程解析+Embedding→入库，立即返回"""
    engine = get_engine()
    results = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            results.append({"filename": f.filename, "ok": False,
                            "error": f"不支持的格式 {ext}"})
            continue
        content = await f.read()

        # 持久化原始文件（供前端直接显示 PDF 原件）
        safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{Path(f.filename).name}"
        stored_path = RESEARCH_FILES_DIR / safe_name
        stored_path.write_bytes(content)

        key = f.filename
        with _import_lock:
            _import_status[key] = {"status": "processing", "title": f.filename, "pending": True}
        # 后台异步处理
        threading.Thread(
            target=_process_report,
            args=(engine, key, f.filename, stored_path),
            daemon=True,
        ).start()
        results.append({"filename": f.filename, "ok": True, "status": "processing"})
    return {"results": results}


def _process_report(engine, key: str, filename: str, stored_path: Path) -> None:
    """后台：解析 → 切块 → Embedding → 入库；更新处理状态。原始文件已持久化，保留供前端显示"""
    try:
        from skills.doc_parser import parse_file
        from skills.research_db import insert_report
        from skills.embedding_client import EmbeddingClient

        parsed = parse_file(str(stored_path))
        parsed["title"]     = parsed.get("title") or Path(filename).stem
        parsed["file_path"] = str(stored_path)   # 持久化的原始文件路径
        embed = EmbeddingClient()
        res = insert_report(engine, embed, parsed)
        rid = str(res.get("report_id"))
        with _import_lock:
            # 用入库后的 id 作为状态 key，列表查询用得到
            _import_status[rid] = {
                "status": "done", "pending": False,
                "chunks": res.get("chunks"), "skipped": res.get("skipped"),
            }
            _import_status.pop(key, None)
        logger.info("[knowledge] 研报导入完成 %s → id=%s chunks=%s", filename, rid, res.get("chunks"))
    except Exception as e:
        logger.error("[knowledge] 研报导入失败 %s: %s", filename, e, exc_info=True)
        # 解析/入库失败则删除已存的原始文件，避免孤立文件
        try:
            stored_path.unlink(missing_ok=True)
        except Exception:
            pass
        with _import_lock:
            _import_status[key] = {"status": "error", "error": str(e),
                                   "title": filename, "pending": True}


@router.get("/search")
def rag_search(q: str = Query(..., min_length=1), industry: str | None = None, top_k: int = 5):
    """RAG 时效加权检索"""
    from agents.research_agent import ResearchAgent

    try:
        hits = ResearchAgent().retrieve(query=q, top_k=top_k, industry=industry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"query": q, "results": to_jsonable(hits)}
