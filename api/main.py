"""
FastAPI 后端：为 React 前端提供数据 API，并托管构建后的前端静态文件。

运行：
  python main.py serve            # 生产：托管 web/dist + /api
  uvicorn api.main:app --reload   # 开发（仅 API，前端用 vite dev server 代理 /api）
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routers import assistant, dashboard, knowledge, research, system

logger = logging.getLogger(__name__)

app = FastAPI(title="DailySM — 行业智能投研平台 API", version="1.0")

# 开发期允许 vite dev server（5173）跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
app.include_router(dashboard.router)
app.include_router(research.router)
app.include_router(knowledge.router)
app.include_router(assistant.router)
app.include_router(system.router)


@app.get("/api/health")
def health():
    return {"ok": True}


# ---- 托管构建后的前端（web/dist）----
DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if DIST.exists():
    assets = DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """SPA 路由回退：所有非 /api 路径返回 index.html"""
        if full_path.startswith("api"):
            return {"detail": "Not Found"}
        candidate = DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")
