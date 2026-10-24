"""B50 报告图片渲染器（Pillow 纯绘制，无需浏览器）

版式对齐 PRP 网站的 B50 导出报告：
- 标题栏：玩家名 / 总 Rating
- B15 段（新曲池）与 B35 段（旧曲池）分节，每节显示均分
- 单元格：曲绘（本地缓存，可缺省色块）+ 曲名 + 难度/定数 + 分数 + Rating
- 三种模式由调用方取数：standard / overflow(underflow=5) / global(best 全池前50)
"""
import asyncio
import io
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------- 配色（暗色主题，对齐 PRP 风格） ----------

BG = (18, 20, 28)
SECTION_BG = (28, 32, 44)
CARD_BG = (38, 44, 60)
CARD_BG_HL = (46, 54, 76)
TEXT = (232, 236, 244)
TEXT_DIM = (150, 158, 176)
ACCENT = (108, 166, 255)

DIFF_COLORS = {
    "detected": (94, 204, 158),
    "invaded": (86, 156, 245),
    "massive": (176, 108, 245),
    "reboot": (240, 158, 82),
}
FALLBACK_COVER = (60, 66, 88)

COVER_URL = "https://prp.icel.site/cover/"


def _font(size: int, bold: bool = False):
    """加载系统字体（Windows 优先微软雅黑）"""
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _rating_to_float(r: int) -> float:
    """PRP rating 是 x100 整数（如 171234 → 1712.34）"""
    return r / 100


def _fmt_rating2(x: float) -> str:
    """保留两位小数但【截断不四舍五入】（165.9856 -> 165.98）"""
    scaled = int(x * 100 + 1e-9)  # 浮点容错
    return f"{scaled // 100}.{scaled % 100:02d}"


def _fmt_score(n: int) -> str:
    return f"{n:,}"


def _load_cover_sync(filename: str, cache_dir: Path, size: int) -> Image.Image | None:
    if not filename:
        return None
    local = cache_dir / filename
    try:
        if not local.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            url = COVER_URL + urllib.parse.quote(filename)
            req = urllib.request.Request(url, headers={"User-Agent": "NympheaBot/0.1"})
            with urllib.request.urlopen(req, timeout=15) as r:
                local.write_bytes(r.read())
        img = Image.open(local).convert("RGB")
        return img.resize((size, size))
    except Exception:
        return None


# ---------- 单元格 ----------

CELL_W = 372
CELL_H = 84
GAP = 10
COLS = 4


def _draw_cell(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int,
               rec: dict, cover: Image.Image | None, font_s, font_xs, fitting: bool = False):
    diff = (rec.get("chart") or {}).get("difficulty", "invaded")
    color = DIFF_COLORS.get(diff, (120, 120, 120))
    # 卡片底
    draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=8, fill=CARD_BG)
    # 左侧难度色条
    draw.rounded_rectangle([x, y, x + 5, y + CELL_H], radius=2, fill=color)
    # 曲绘
    if cover:
        img.paste(cover, (x + 14, y + 12))
    else:
        draw.rounded_rectangle([x + 14, y + 12, x + 14 + 60, y + 12 + 60], radius=4, fill=FALLBACK_COVER)
        draw.ellipse([x + 34, y + 34, x + 44, y + 44], fill=TEXT_DIM)  # 缺省占位圆点
    # 文本区
    tx = x + 86
    chart = rec.get("chart") or {}
    title = (chart.get("title") or "?")[:16]
    fit_mark = "*" if chart.get("fitting_rating") is not None and chart.get("fitting_level") is None else ""
    draw.text((tx, y + 10), f"{title}{fit_mark}", fill=TEXT, font=font_s)
    if fitting and chart.get("fitting_level") is not None:
        lv = chart["fitting_level"]  # 拟合模式显示拟合定数
    else:
        lv = chart.get("level") or 0  # 标准/全局模式：官方定数
    draw.text((tx, y + 34), f"{diff} {lv:g}", fill=color, font=font_xs)
    draw.text((tx, y + 52), _fmt_score(int(rec.get("score") or 0)), fill=TEXT_DIM, font=font_xs)
    if fitting and rec.get("fitting_rating") is not None:
        rating = rec["fitting_rating"]
    else:
        rating = rec.get("rating") or 0
    draw.text((x + CELL_W - 10, y + 12), _fmt_rating2(_rating_to_float(rating)),
              fill=ACCENT, font=font_s, anchor="ra")


def _render(sections: list[dict], username: str, nickname: str, total_rating: float,
            title: str, covers: dict, out_path: Path, footer: str = "", fitting: bool = False) -> Path:
    """sections: [{"label": "Best 15", "avg": 1712.34, "records": [...], "cutoff": 15}]"""
    f_title = _font(30, bold=True)
    f_sub = _font(18)
    f_sec = _font(22, bold=True)
    f_s = _font(16, bold=False)
    f_xs = _font(14)
    f_big = _font(40, bold=True)

    margin = 28
    header_h = 110
    sec_head_h = 56
    width = margin * 2 + COLS * CELL_W + (COLS - 1) * GAP

    # 预计算高度
    heights = []
    for sec in sections:
        rows = (len(sec["records"]) + COLS - 1) // COLS
        heights.append(sec_head_h + rows * (CELL_H + GAP) + 12)
    total_h = header_h + sum(heights) + margin * 2 + (30 if footer else 0)

    img = Image.new("RGB", (width, total_h), BG)
    draw = ImageDraw.Draw(img)

    # 头部
    draw.text((margin, 24), title, fill=TEXT, font=f_title)
    draw.text((margin, 66), f"{nickname or username}  ·  {footer or 'Rating Report'}", fill=TEXT_DIM, font=f_sub)
    draw.text((width - margin, 30), _fmt_rating2(total_rating), fill=ACCENT, font=f_big, anchor="ra")
    draw.text((width - margin, 78), "TOTAL RATING", fill=TEXT_DIM, font=f_xs, anchor="ra")
    draw.line([(margin, header_h - 12), (width - margin, header_h - 12)], fill=(52, 58, 78), width=2)

    # 分节
    y = header_h + margin // 2
    for sec, h in zip(sections, heights):
        draw.text((margin, y + 8), sec["label"], fill=TEXT, font=f_sec)
        draw.text((width - margin, y + 10), f"AVG {_fmt_rating2(sec['avg'])}", fill=ACCENT, font=f_sub, anchor="ra")
        y += sec_head_h
        for i, rec in enumerate(sec["records"]):
            row, col = divmod(i, COLS)
            x = margin + col * (CELL_W + GAP)
            cy = y + row * (CELL_H + GAP)
            cover = covers.get((rec.get("chart") or {}).get("cover") or "")
            _draw_cell(img, draw, x, cy, rec, cover, f_s, f_xs, fitting)
        y += h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=92)
    return out_path


async def render_b50_image(username: str, nickname: str, sections: list[dict],
                           total_rating: float, title: str, out_path: Path,
                           footer: str = "", fitting: bool = False) -> Path:
    """异步入口：并发拉曲绘后渲染"""
    cache_dir = out_path.parent / "covers"
    all_recs = [r for sec in sections for r in sec["records"]]

    def _fetch_all():
        covers = {}
        for r in all_recs:
            fn = (r.get("chart") or {}).get("cover") or ""
            if fn and fn not in covers:
                covers[fn] = _load_cover_sync(fn, cache_dir, 60)
        return covers

    covers = await asyncio.to_thread(_fetch_all)
    return await asyncio.to_thread(
        _render, sections, username, nickname, total_rating, title, covers, out_path, footer, fitting
    )
