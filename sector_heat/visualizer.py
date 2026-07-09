"""
热力图可视化模块

生成「板块热度雷达」热力矩阵图：
  - 横轴：最近 N 个交易日（日期）
  - 纵轴：行业板块（按最新热度降序）
  - 单元格颜色：蓝(0) → 白(50) → 红(100)
  - 单元格内显示热度数值

同时输出 JSON 数据文件（供 Canvas 或其他前端消费）。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import logging

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")   # 无界面后端，Windows 无需 display
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.font_manager as fm
import yaml

# ---------------------------------------------------------------------------
# Windows 中文字体配置（优先使用系统自带字体，避免乱码）
# ---------------------------------------------------------------------------
def _setup_chinese_font() -> None:
    for font_name in ("Microsoft YaHei", "SimHei", "SimSun", "FangSong"):
        try:
            fonts = [f for f in fm.findSystemFonts() if font_name.lower().replace(" ", "") in
                     f.lower().replace(" ", "").replace("-", "")]
            if fonts:
                matplotlib.rcParams["font.family"] = "sans-serif"
                matplotlib.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
                matplotlib.rcParams["axes.unicode_minus"] = False
                return
        except Exception:
            pass
    # 兜底：尝试设置 Microsoft YaHei 即使 findSystemFonts 没找到
    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

_setup_chinese_font()

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_PATH  = _PROJECT_ROOT / "config" / "config.yaml"


def _load_viz_cfg() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f).get("sector_heat", {}).get("visualizer", {})


# ---------------------------------------------------------------------------
# 热力矩阵图
# ---------------------------------------------------------------------------

def draw_heatmap(heat_rows: list[dict], output_dir: Path | None = None) -> Path:
    """
    接收 sector_heat_index 记录列表，绘制热力图并保存 PNG。

    Args:
        heat_rows:  [{"trade_date": date, "sector_name": str, "heat_score": float}, ...]
        output_dir: 输出目录（None 时从 config 读取）

    Returns:
        PNG 文件路径
    """
    cfg = _load_viz_cfg()
    lookback   = cfg.get("lookback_days", 20)
    sort_by    = cfg.get("sort_by", "latest_heat")
    dpi        = cfg.get("dpi", 150)
    out_dir    = output_dir or (_PROJECT_ROOT / cfg.get("output_dir", "data/heatmaps"))
    out_dir.mkdir(parents=True, exist_ok=True)

    if not heat_rows:
        logger.warning("热力图数据为空，跳过绘制")
        return Path()

    df = pd.DataFrame(heat_rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["heat_score"] = pd.to_numeric(df["heat_score"], errors="coerce").fillna(0)
    if "sort_score" in df.columns:
        df["sort_score"] = pd.to_numeric(df["sort_score"], errors="coerce").fillna(0)

    # 取最近 N 个交易日
    recent_dates = sorted(df["trade_date"].unique())[-lookback:]
    df = df[df["trade_date"].isin(recent_dates)]

    # 转为矩阵（行=板块，列=日期）— 颜色用 heat_score
    pivot = df.pivot_table(index="sector_name", columns="trade_date", values="heat_score", aggfunc="first")
    pivot = pivot.reindex(columns=sorted(pivot.columns))

    # 板块排序：优先用最新日期的 sort_score，其次退化为 heat_score
    latest_date = sorted(recent_dates)[-1] if recent_dates else None
    if latest_date is not None and "sort_score" in df.columns:
        latest_sort = (
            df[df["trade_date"] == latest_date]
            .set_index("sector_name")["sort_score"]
        )
        sector_order = latest_sort.reindex(pivot.index).fillna(0).sort_values(ascending=False).index
        pivot = pivot.reindex(sector_order)
    elif sort_by == "latest_heat" and len(pivot.columns) > 0:
        latest_col = pivot.columns[-1]
        pivot = pivot.sort_values(latest_col, ascending=False)

    n_sectors = len(pivot)
    n_dates   = len(pivot.columns)

    # ---- 构造自定义颜色映射：蓝-白-红 ----
    cmap = _make_bwr_cmap()

    # ---- 绘图尺寸 ----
    fig_w = max(12, n_dates * 0.85 + 3)
    fig_h = max(8,  n_sectors * 0.40 + 2)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    mat = pivot.values.astype(float)
    im  = ax.imshow(mat, cmap=cmap, vmin=0, vmax=100, aspect="auto")

    # ---- 动态字号：数字高度约占单元格高度 3/4 ----
    # tight_layout 后有效绘图区约占 fig_h 的 85%；换算为 pt（1 inch = 72 pt）
    cell_h_pts  = (fig_h * 0.85 / n_sectors) * 72
    num_fontsize = max(5, min(22, cell_h_pts * 0.72))   # 0.72 ≈ 3/4 留余量

    # 单元格数值标注
    for i in range(n_sectors):
        for j in range(n_dates):
            val = mat[i, j]
            if np.isnan(val):
                continue
            text_color = "white" if val < 20 or val > 80 else "#222"
            ax.text(j, i, f"{val:.0f}", ha="center", va="center",
                    fontsize=num_fontsize, color=text_color, fontweight="bold")

    # 轴标签（x 轴日期抽稀：当列数较多时只显示部分日期）
    date_labels = [str(d)[5:] for d in pivot.columns]  # MM-DD 格式
    # 目标：最多显示约 25 个日期标签，优先显示每月第一个交易日
    max_labels = 25
    if n_dates <= max_labels:
        tick_pos    = list(range(n_dates))
        tick_labels = date_labels
    else:
        step = max(1, n_dates // max_labels)
        tick_pos    = list(range(0, n_dates, step))
        tick_labels = [date_labels[i] for i in tick_pos]

    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=8, color="white")
    ax.set_yticks(range(n_sectors))
    ax.set_yticklabels(pivot.index.tolist(), fontsize=num_fontsize, color="white")

    ax.tick_params(colors="white", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)

    # 网格线
    ax.set_xticks(np.arange(-0.5, n_dates, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, n_sectors, 1), minor=True)
    ax.grid(which="minor", color="#2a2a2a", linewidth=0.5)
    ax.tick_params(which="minor", bottom=False, left=False)

    # 标题与色标
    today_str = str(recent_dates[-1]) if recent_dates else ""
    ax.set_title(f"A股行业板块热度雷达  |  最近{n_dates}个交易日  |  {today_str}",
                 color="white", fontsize=13, pad=14, fontweight="bold")

    cbar = fig.colorbar(im, ax=ax, orientation="vertical", fraction=0.015, pad=0.02)
    cbar.ax.tick_params(colors="white", labelsize=8)
    cbar.set_label("热度指数", color="white", fontsize=8)
    cbar.outline.set_edgecolor("#444")

    fig.tight_layout(pad=1.5)

    # 保存
    out_path = out_dir / f"heatmap_{today_str}.png"
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("热力图已保存: %s", out_path)

    # 顺便导出 JSON
    export_json(heat_rows, out_dir / f"heatmap_{today_str}.json")

    return out_path


def export_json(heat_rows: list[dict], json_path: Path) -> None:
    """导出热力矩阵数据为 JSON，供 Canvas 或前端使用"""
    import decimal

    def _default(obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    serializable = [
        {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in r.items()}
        for r in heat_rows
    ]
    json_path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=_default),
        encoding="utf-8",
    )
    logger.info("JSON 数据已导出: %s", json_path)


# ---------------------------------------------------------------------------
# 颜色工具
# ---------------------------------------------------------------------------

def _make_bwr_cmap() -> mcolors.LinearSegmentedColormap:
    """蓝(0) → 白(50) → 红(100) 颜色映射"""
    colors_list = [
        (0.0,  "#1565C0"),   # 深蓝
        (0.25, "#5BADF0"),   # 浅蓝
        (0.50, "#F5F5F5"),   # 白
        (0.75, "#EF6C6C"),   # 浅红
        (1.0,  "#C62828"),   # 深红
    ]
    return mcolors.LinearSegmentedColormap.from_list(
        "sector_heat", [(v, c) for v, c in colors_list]
    )
