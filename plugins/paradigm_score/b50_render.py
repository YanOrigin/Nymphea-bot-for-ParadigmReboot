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
CELL_H = 216   # 拉高：容纳底部 AD/FC 徽章行
GAP = 15
COLS = 5


# ---------- 图标资源（data/icons/） ----------

def _find_icon_dir() -> Path:
    r"""图标目录：自适应 bot 副本与 Tracker core 两种位置

    bot 副本   : D:\Developed\Nymphea-bot\plugins\paradigm_score\b50_render.py
                 → parents[2] = D:\Developed\Nymphea-bot  → data/icons ✓
    Tracker core: D:\Developed\ParadigmReboot-Tracker\core\b50_render.py
                 → parents[2] = D:\Developed  → 需再拼 Nymphea-bot/data/icons
    """
    here = Path(__file__).resolve()
    p2 = here.parents[2] if len(here.parents) > 2 else here.parent
    candidates = [
        p2 / "data" / "icons",                       # bot 副本
        p2 / "Nymphea-bot" / "data" / "icons",       # Tracker core（同级有 Nymphea-bot）
        here.parent / "icons",                       # 万一放在 core/icons
        Path(r"D:\Developed\Nymphea-bot\data\icons"),  # 绝对兜底
    ]
    for c in candidates:
        try:
            if c.exists() and next(c.rglob("*.png"), None) is not None:
                return c
        except OSError:
            continue
    return candidates[-1]


ICON_DIR = _find_icon_dir()
_icon_cache: dict[str, Image.Image] = {}

# 评级标识文件名前缀（从高到低）
RANK_ORDER = ("MAAAPLUS", "MAAA", "MAAPLUS", "MAA", "MAPLUS", "MA", "MB", "MC", "MD", "MINFPLUS", "MINF")

# 评级分数段（用户提供，从高到低匹配：分数 >= 阈值即该档）
# 注：MB / MC / MD 三档的分数线用户尚未提供，暂按每档递减 30,000 估算，待补正
RANK_THRESHOLDS: list[tuple[str, int]] = [
    ("MINFPLUS", 1009000),   # 1,009,000 ~ 1,010,000（最高）
    ("MINF", 1000000),       # 1,000,000 ~ 1,008,999（跨百万）
    ("MAAAPLUS", 990000),    #   990,000 ~   999,999
    ("MAAA", 980000),        #   980,000 ~   989,999
    ("MAAPLUS", 970000),     #   970,000 ~   979,999
    ("MAA", 950000),         #   950,000 ~   969,999
    ("MAPLUS", 930000),      #   930,000 ~   949,999
    ("MA", 900000),          #   900,000 ~   929,999（A，90 万）
    ("MB", 850000),          #   850,000 ~   899,999（B）
    ("MC", 800000),          #   800,000 ~   849,999（C）
    ("MD", 0),               #         0 ~   799,999（D）
]


def _grade_for_score(score: int) -> str:
    """分数 → 评级标识名（MD 阈值为 0，必然命中，无需兜底）"""
    for name, threshold in RANK_THRESHOLDS:
        if score >= threshold:
            return name
    return "MD"


def _load_icon(kind: str, name: str) -> Image.Image | None:
    """加载图标（kind: 'rank' | 'achievement'），带缓存。name 可含或不含数字后缀"""
    key = f"{kind}/{name}"
    if key in _icon_cache:
        return _icon_cache[key]
    d = ICON_DIR / ("rank" if kind == "rank" else "achievement")
    if not d.exists():
        return None
    # 精确命中；否则按前缀找（文件名形如 MAAAPLUS_350.png）
    cand = None
    exact = d / f"{name}.png"
    if exact.exists():
        cand = exact
    else:
        for p in d.glob("*.png"):
            if p.stem.split("_")[0] == name:
                cand = p
                break
    if not cand:
        return None
    try:
        im = Image.open(cand).convert("RGBA")
        _icon_cache[key] = im
        return im
    except Exception:
        return None


def _paste_icon(img: Image.Image, icon: Image.Image | None, box: tuple[int, int, int, int]):
    """等比缩放贴图到指定矩形（居中对齐），保持透明"""
    if icon is None:
        return
    bx, by, bw, bh = box
    ratio = min(bw / icon.width, bh / icon.height)
    nw, nh = max(1, int(icon.width * ratio)), max(1, int(icon.height * ratio))
    resized = icon.resize((nw, nh), Image.LANCZOS)
    img.paste(resized, (bx + (bw - nw) // 2, by + (bh - nh) // 2), resized)


def _truncate(text: str, font, max_w: int) -> str:
    """单行截断：超出可用宽度就在单元格结尾处截断并补省略号（不换行）"""
    if _text_w(text, font) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_w(text[:mid] + "…", font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo > 0 else "…"


def _text_w(text: str, font) -> int:
    try:
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]
    except AttributeError:
        return font.getsize(text)[0]


def _draw_cell(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int,
               rec: dict, cover: Image.Image | None, fonts: dict,
               fitting: bool = False, rank: int = 0):
    """单格版式（左立绘 + 右文字）：

        ┌──────┬──────────────────────────────┐
        │      │ #7  spellofchaos             │  ← 排位 + wiki_id
        │ 曲绘 │ Spell of chaoS               │  ← 曲名（超宽截断）
        │      │ 1,009,526                    │  ← 分数（大字）
        │      │ 16.5 → 173.26  [评级][AD/FC] │  ← 定数 → rating + 图标
        └──────┴──────────────────────────────┘
    """
    f_rankno = fonts["rankno"]
    f_title = fonts["title"]
    f_score = fonts["score"]
    f_meta = fonts["meta"]

    chart = rec.get("chart") or {}
    diff = chart.get("difficulty", "invaded")
    color = DIFF_COLORS.get(diff, (120, 120, 120))
    score = int(rec.get("score") or 0)

    # 卡片底 + 左侧难度色条
    draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=12, fill=CARD_BG)
    draw.rounded_rectangle([x, y, x + 7, y + CELL_H], radius=3, fill=color)

    # ---- 左：曲绘 ----
    CV = 152
    cv_x, cv_y = x + 20, y + 32
    if cover:
        img.paste(cover, (cv_x, cv_y))
    else:
        draw.rounded_rectangle([cv_x, cv_y, cv_x + CV, cv_y + CV], radius=8, fill=FALLBACK_COVER)
        cr = cv_x + CV // 2
        draw.ellipse([cr - 14, cv_y + CV // 2 - 14, cr + 14, cv_y + CV // 2 + 14], fill=TEXT_DIM)

    # ---- 右：文字列 ----
    tx = x + 20 + CV + 20
    right_pad = 20
    text_w = CELL_W - (20 + CV + 20) - right_pad

    # 行1：#排位 + 空格 + wiki_id
    wid = chart.get("wiki_id") or ""
    head = f"#{rank}" if rank else "#--"
    if wid:
        head = f"{head}  {wid}"
    draw.text((tx, y + 16), _truncate(head, f_rankno, text_w), fill=TEXT_DIM, font=f_rankno)

    # 行2：曲名（单行，超出直接截断）
    title = chart.get("title") or "?"
    fit_mark = "*" if chart.get("fitting_rating") is not None and chart.get("fitting_level") is None else ""
    draw.text((tx, y + 44), _truncate(f"{title}{fit_mark}", f_title, text_w), fill=TEXT, font=f_title)

    # 行3：分数
    draw.text((tx, y + 80), _fmt_score(score), fill=TEXT, font=f_score)

    # 行4：定数 → 单曲 rating，紧跟评级图标 + 成就图标（依次排布，不右对齐）
    if fitting and chart.get("fitting_level") is not None:
        lv = chart["fitting_level"]
    else:
        lv = chart.get("level") or 0
    if fitting and rec.get("fitting_rating") is not None:
        rating = rec["fitting_rating"]
    else:
        rating = rec.get("rating") or 0
    meta = f"{lv:g} → {_fmt_rating2(_rating_to_float(rating))}"

    row4_y = y + 148
    ICON_H = 40
    icon_y = row4_y - 6
    cur_x = tx

    # 文字：可用宽度预留两个图标位，避免图标被挤出版面
    rank_icon = _load_icon("rank", _grade_for_score(score))
    ach_icon, _ = _achievement_icon(rec)
    reserve = 0
    if rank_icon is not None:
        reserve += int(ICON_H * rank_icon.width / rank_icon.height) + 14
    if ach_icon is not None:
        reserve += int(ICON_H * ach_icon.width / ach_icon.height) + 14

    meta_txt = _truncate(meta, f_meta, max(40, text_w - reserve))
    draw.text((cur_x, row4_y + 2), meta_txt, fill=color, font=f_meta)
    cur_x += _text_w(meta_txt, f_meta) + 14

    if rank_icon is not None:
        rw = int(ICON_H * rank_icon.width / rank_icon.height)
        _paste_icon(img, rank_icon, (cur_x, icon_y, rw, ICON_H))
        cur_x += rw + 14

    if ach_icon is not None:
        aw = int(ICON_H * ach_icon.width / ach_icon.height)
        _paste_icon(img, ach_icon, (cur_x, icon_y, aw, ICON_H))


def _achievement_icon(rec: dict) -> tuple[Image.Image | None, str]:
    """AD/FC 成就图标。AD 与 FC 是【覆盖关系】：AD 成立的曲目只显示 AD。

    仅在有真实判定数据时显示（无缓存时返回空，避免误导）。
    """
    judge = rec.get("_judge")
    if not judge:
        return None, ""
    if int(judge.get("lost", 0)) != 0:
        return None, ""                              # 有 Miss：两者都不成立
    if int(judge.get("received", 0)) == 0:
        return _load_icon("achievement", "AD徽章小图"), "AD"
    return _load_icon("achievement", "FC徽章小图"), "FC"


def _render(sections: list[dict], username: str, nickname: str, total_rating: float,
            title: str, covers: dict, out_path: Path, footer: str = "", fitting: bool = False,
            query_time: str | None = None, hide_total: bool = False) -> Path:
    """sections: [{"label": "Best 35", "avg": 166.7, "records": [...]}]"""
    f_title = _font(45, bold=True)
    f_sub = _font(27)
    f_sec = _font(33, bold=True)
    f_big = _font(56, bold=True)
    f_label = _font(21)
    # 单格字号
    cell_fonts = {
        "rankno": _font(21),          # #排位 + wiki_id
        "title": _font(26),           # 曲名
        "score": _font(38, bold=True),  # 分数（大字）
        "meta": _font(24),            # 定数 → rating
    }

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
            # 排位：本节内的序号（1-based），与图上展示顺序一致
            _draw_cell(img, draw, x, row_y, rec, cover, cell_fonts, fitting, rank=i + 1)
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
        _render, sections, username, nickname, total_rating, title, covers, out_path, footer,
        fitting, query_time, hide_total
    )
