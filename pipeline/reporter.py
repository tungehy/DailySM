"""
报告生成 & 状态数据库模块

负责：
1. 将 LLM 生成的 Markdown 报告写入 data/reports/
2. 用 SQLite 记录每个视频的处理状态，避免重复下载和转录
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
import logging

from .config import config
from .summarizer import SummaryResult

logger = logging.getLogger(__name__)


# ============================================================
# 状态数据库
# ============================================================

class StateDB:
    """
    SQLite 数据库，记录每条视频的处理状态。

    表结构：
        videos(
            bvid TEXT PRIMARY KEY,
            title TEXT,
            up_uid TEXT,
            up_name TEXT,
            pub_date TEXT,          -- ISO 格式
            downloaded_at TEXT,     -- NULL=未下载
            transcribed_at TEXT,    -- NULL=未转录
            audio_path TEXT,
            transcript_path TEXT,
            report_date TEXT        -- 归属报告日期（NULL=未进入报告）
        )
    """

    def __init__(self):
        self._path = config.db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    bvid             TEXT PRIMARY KEY,
                    title            TEXT,
                    up_uid           TEXT,
                    up_name          TEXT,
                    pub_date         TEXT,
                    downloaded_at    TEXT,
                    transcribed_at   TEXT,
                    audio_path       TEXT,
                    transcript_path  TEXT,
                    report_date      TEXT
                )
                """
            )

    # ---- 写入 ----

    def upsert_video(
        self,
        bvid: str,
        title: str = "",
        up_uid: str = "",
        up_name: str = "",
        pub_date: str = "",
    ):
        """插入或更新视频基础信息（幂等）"""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO videos (bvid, title, up_uid, up_name, pub_date)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    title    = excluded.title,
                    up_uid   = excluded.up_uid,
                    up_name  = excluded.up_name,
                    pub_date = excluded.pub_date
                """,
                (bvid, title, up_uid, up_name, pub_date),
            )

    def mark_downloaded(self, bvid: str, audio_path: str):
        ts = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "UPDATE videos SET downloaded_at=?, audio_path=? WHERE bvid=?",
                (ts, audio_path, bvid),
            )

    def mark_transcribed(self, bvid: str, transcript_path: str):
        ts = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "UPDATE videos SET transcribed_at=?, transcript_path=? WHERE bvid=?",
                (ts, transcript_path, bvid),
            )

    def mark_reported(self, bvid: str, report_date: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE videos SET report_date=? WHERE bvid=?",
                (report_date, bvid),
            )

    # ---- 查询 ----

    def is_downloaded(self, bvid: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT downloaded_at FROM videos WHERE bvid=?", (bvid,)
            ).fetchone()
        return bool(row and row["downloaded_at"])

    def is_transcribed(self, bvid: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT transcribed_at FROM videos WHERE bvid=?", (bvid,)
            ).fetchone()
        return bool(row and row["transcribed_at"])

    def list_all(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM videos ORDER BY pub_date DESC").fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 报告写入
# ============================================================

class ReportWriter:
    """将 SummaryResult 写入 Markdown 文件（可选 HTML）"""

    def __init__(self):
        config.report_dir.mkdir(parents=True, exist_ok=True)

    def write(self, result: SummaryResult) -> Path | None:
        """
        将报告写入文件并返回文件路径。
        失败时返回 None。
        """
        if not result.success or not result.markdown_content:
            logger.error("报告内容为空或生成失败，跳过写入")
            return None

        filename = f"{result.report_date.strftime('%Y-%m-%d')}.md"
        out_path = config.report_dir / filename
        out_path.write_text(result.markdown_content, encoding="utf-8")
        logger.info("报告已写入: %s", out_path)

        if config.generate_html:
            html_path = self._write_html(result, out_path)
            if html_path:
                logger.info("HTML 报告已写入: %s", html_path)

        return out_path

    @staticmethod
    def _write_html(result: SummaryResult, md_path: Path) -> Path | None:
        """将 Markdown 转换为简单 HTML（需安装 markdown 库）"""
        try:
            import markdown as md_lib
        except ImportError:
            logger.warning("未安装 markdown 库，跳过 HTML 生成。pip install markdown")
            return None

        html_content = md_lib.markdown(
            result.markdown_content,
            extensions=["tables", "fenced_code"],
        )
        page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{config.report_title_prefix} {result.report_date}</title>
<style>
  body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 20px;
         color: #222; line-height: 1.7; }}
  h1 {{ color: #c0392b; }}
  h2 {{ border-bottom: 2px solid #eee; padding-bottom: 4px; }}
  code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; }}
</style>
</head>
<body>
{html_content}
</body>
</html>"""
        html_path = md_path.with_suffix(".html")
        html_path.write_text(page, encoding="utf-8")
        return html_path

    def list_reports(self) -> list[Path]:
        """列出所有已生成的报告文件"""
        return sorted(config.report_dir.glob("*.md"), reverse=True)

    def get_latest_report(self) -> Path | None:
        reports = self.list_reports()
        return reports[0] if reports else None
