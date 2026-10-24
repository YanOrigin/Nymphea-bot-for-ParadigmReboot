"""B50 报告图片渲染器（Pillow 纯绘制，无需浏览器）

版式对齐 PRP 网站的 B50 导出报告：
- 标题栏：玩家名 / 总 Rating
- B15 段（新曲池）与 B35 段（旧曲池）分节，每节显示均分
- 单元格：曲绘（本地缓存，可缺省色块）+ 曲名 + 难度/定数 + 分数 + Rating
- 三种模式由调用方取数：standard / overflow(underflow=5) / global(best 全池前50)
"""
import asyncio
import io
from datetime import datetime
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
    "detected": (86, 156, 245),   # 原 invaded 蓝
    "invaded": (218, 41, 28),     # PANTONE 485 C (#DA291C)
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

CELL_W = 558   # 372 * 1.5
CELL_H = 164   # 曲绘 144 + 上下各 10 内边距
GAP = 15
COLS = 5


def _needs_two_lines(rec: dict) -> bool:
    """该记录曲名是否需要两行（预判用，渲染时同样逻辑）"""
    return False  # 占位：真实判断在渲染时由 _wrap_title 决定，这里保守返回 False 保持行高

def _wrap_title(text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
    """曲名按像素宽度换行；最多 max_lines 行，超出末行加省略号。逐字符断行（适配中日文无空格）"""
    if _text_w(text, font) <= max_w:
        return [text]
    lines, cur = [], ""
    for ch in text:
        if _text_w(cur + ch, font) > max_w:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                break
        else:
            cur += ch
    if len(lines) < max_lines and cur:
        lines.append(cur)
    # 超出：末行截断 + 省略号
    if len(lines) == max_lines and (len(lines_ := lines) < 1 or _text_w(lines[-1], font) > max_w or _still_overflow(text, lines, font, max_w)):
        last = lines[-1]
        while last and _text_w(last + "…", font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines[:max_lines]


def _still_overflow(original: str, lines: list[str], font, max_w: int) -> bool:
    """判断是否还有未展示的文本（用于决定是否加省略号）"""
    consumed = "".join(lines)
    return len(consumed) < len(original)


def _text_w(text: str, font) -> int:
    try:
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]
    except AttributeError:
        return font.getsize(text)[0]


def _draw_cell(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int,
               rec: dict, cover: Image.Image | None, f_title, f_small, f_rating,
               fitting: bool = False):
    diff = (rec.get("chart") or {}).get("difficulty", "invaded")
    color = DIFF_COLORS.get(diff, (120, 120, 120))
    # 卡片底
    draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=12, fill=CARD_BG)
    # 左侧难度色条
    draw.rounded_rectangle([x, y, x + 7, y + CELL_H], radius=3, fill=color)
    # 曲绘 144x144（1.5x）
    CV = 144
    cv_x = x + 18
    cv_y = y + (CELL_H - CV) // 2  # 居中，上下各留 10px
    if cover:
        img.paste(cover, (cv_x, cv_y))
    else:
        draw.rounded_rectangle([cv_x, cv_y, cv_x + CV, cv_y + CV], radius=8, fill=FALLBACK_COVER)
        cr = cv_x + CV // 2
        draw.ellipse([cr - 12, cv_y + CV // 2 - 12, cr + 12, cv_y + CV // 2 + 12], fill=TEXT_DIM)
    # 文本区
    tx = x + 18 + CV + 18
    chart = rec.get("chart") or {}
    title = chart.get("title") or "?"
    fit_mark = "*" if chart.get("fitting_rating") is not None and chart.get("fitting_level") is None else ""
    title_full = f"{title}{fit_mark}"
    max_w = CELL_W - (18 + 144 + 18) - 18
    lines = _wrap_title(title_full, f_title, max_w, max_lines=2)
    draw.text((tx, y + 8), lines[0], fill=TEXT, font=f_title)
    if len(lines) > 1:
        draw.text((tx, y + 36), lines[1], fill=TEXT, font=f_title)
        lv_y, score_y = y + 72, y + 108
    else:
        lv_y, score_y = y + 60, y + 100
    if fitting and chart.get("fitting_level") is not None:
        lv = chart["fitting_level"]
    else:
        lv = chart.get("level") or 0
    draw.text((tx, lv_y), f"{diff} {lv:g}", fill=color, font=f_small)
    # 第三行：分数（左） + 单曲 rating（右）
    draw.text((tx, score_y), _fmt_score(int(rec.get("score") or 0)), fill=TEXT_DIM, font=f_small)
    if fitting and rec.get("fitting_rating") is not None:
        rating = rec["fitting_rating"]
    else:
        rating = rec.get("rating") or 0
    draw.text((x + CELL_W - 18, score_y + 2), _fmt_rating2(_rating_to_float(rating)),
              fill=ACCENT, font=f_rating, anchor="ra")


def _render(sections: list[dict], username: str, nickname: str, total_rating: float,
            title: str, covers: dict, out_path: Path, footer: str = "", fitting: bool = False,
            query_time: str | None = None, hide_total: bool = False) -> Path:
    """sections: [{"label": "Best 35", "avg": 166.7, "records": [...]}]"""
    f_title = _font(45, bold=True)
    f_sub = _font(27)
    f_sec = _font(33, bold=True)
    f_big = _font(56, bold=True)
    f_label = _font(21)
    # 单元格字号：曲名 16*1.2≈19pt，其余 18/20 *1.5 = 27/30pt
    f_cell_title = _font(19)
    f_cell_small = _font(27)
    f_cell_rating = _font(30)

    margin = 40
    header_h = 172
    sec_head_h = 78
    width = margin * 2 + COLS * CELL_W + (COLS - 1) * GAP

    heights = []
    for sec in sections:
        rows = (len(sec["records"]) + COLS - 1) // COLS
        heights.append(sec_head_h + rows * (CELL_H + GAP) + 16)
    total_h = header_h + sum(heights) + margin * 2 + (44 if footer else 0)

    img = Image.new("RGB", (width, total_h), BG)
    draw = ImageDraw.Draw(img)

    # ---- 头部：左上角查询时间 ----
    ts = query_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    draw.text((margin, 12), f"查询时间 {ts}", fill=TEXT_DIM, font=f_label)
    draw.text((margin, 46), title, fill=TEXT, font=f_title)
    draw.text((margin, 112), f"{nickname or username}  ·  {footer or 'Rating Report'}", fill=TEXT_DIM, font=f_sub)
    if not hide_total:
        draw.text((width - margin, 40), _fmt_rating2(total_rating), fill=ACCENT, font=f_big, anchor="ra")
        draw.text((width - margin, 112), "TOTAL RATING", fill=TEXT_DIM, font=f_label, anchor="ra")
    # 分隔线：位于昵称行下方（昵称 112 + 字高 36 = 148，留 24 余量）
    draw.line([(margin, 172), (width - margin, 172)], fill=(52, 58, 78), width=3)

    # ---- 分节 ----
    y = header_h + margin // 2
    for sec, h in zip(sections, heights):
        draw.text((margin, y + 10), sec["label"], fill=TEXT, font=f_sec)
        if not hide_total:
            draw.text((width - margin, y + 18), f"AVG {_fmt_rating2(sec['avg'])}", fill=ACCENT, font=f_sub, anchor="ra")
        y += sec_head_h
        row_y = y
        for i, rec in enumerate(sec["records"]):
            row, col = divmod(i, COLS)
            if col == 0 and row > 0:
                row_y += CELL_H + GAP
            x = margin + col * (CELL_W + GAP)
            cover = covers.get((rec.get("chart") or {}).get("cover") or "")
            _draw_cell(img, draw, x, row_y, rec, cover, f_cell_title, f_cell_small, f_cell_rating, fitting)
        y += h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=90)
    return out_path


async def render_b50_image(username: str, nickname: str, sections: list[dict],
                           total_rating: float, title: str, out_path: Path,
                           footer: str = "", fitting: bool = False,
                           query_time: str | None = None, hide_total: bool = False) -> Path:
    """异步入口：并发拉曲绘后渲染"""
    cache_dir = out_path.parent / "covers"
    all_recs = [r for sec in sections for r in sec["records"]]

    def _fetch_all():
        covers = {}
        for r in all_recs:
            fn = (r.get("chart") or {}).get("cover") or ""
            if fn and fn not in covers:
                covers[fn] = _load_cover_sync(fn, cache_dir, 144)
        return covers

    covers = await asyncio.to_thread(_fetch_all)
    return await asyncio.to_thread(
        _render, sections, username, nickname, total_rating, title, covers, out_path, footer, fitting, query_time, hide_total
    )
