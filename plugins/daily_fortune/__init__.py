"""今日运势插件（复刻幽幽子 bot 风格 + 范式：起源今日曲目）

指令：
    /今日运势   抽取当日运势（每人每日一次，0点刷新）

特性：
    - 六档运势：上善 / 中庸 / 安平 / 无恙 / 染秽 / 穷凶（后两档为"凶"）
    - 综合分数 + 三分项（桃花运 / 打歌运 / 事业运）星级
    - 昨日抽过且为凶、今日抽到非凶 → 附加"恭喜你脱离了凶"祝福
    - 今日曲目：从范式：起源曲库随机一首，输出曲绘 + 曲名 + 曲师 + 三难度定数
    - 结果以 种子=日期+QQ号 的确定性随机生成（同日重复请求结果一致）
"""
import asyncio
import json
import random
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from nonebot import on_command
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata

from .config import Config, config

__plugin_meta__ = PluginMetadata(
    name="daily_fortune",
    description="今日运势（六档 + 分项 + 脱凶祝福 + 范式：起源今日曲目）",
    usage="/今日运势",
    type="application",
    homepage=None,
    config=Config,
    supported_adapters={"~onebot.v11"},
)

# ---------- 常量 ----------

# 六档运势（索引 0~5），后两档为"凶"
LUCK_TIERS = ["上善", "中庸", "安平", "无恙", "染秽", "穷凶"]
EVIL_TIERS = {4, 5}  # 染秽 / 穷凶

STAR_FULL, STAR_EMPTY = "★", "☆"
ASPECTS = ["桃花运", "打歌运", "事业运"]

# 每档位分数区间（左闭右开），与六档一一对应
TIER_SCORE_RANGES = {
    0: (95, 101),   # 上善
    1: (75, 95),    # 中庸
    2: (55, 75),    # 安平
    3: (35, 55),    # 无恙
    4: (15, 35),    # 染秽
    5: (0, 15),     # 穷凶
}

AGAINST_GREED_TEXT = "今天已经抽过签了哟，贪心的人para不会祝福的哼"
ESCAPE_EVIL_TEXT = "恭喜你脱离了凶，今天也干劲满满呢~"

# ---------- 存储 ----------

_DATA_FILE = Path(config.fortune_data_file)


def _load_data() -> dict:
    if not _DATA_FILE.exists():
        return {"users": {}}
    try:
        return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"users": {}}


def _save_data(data: dict) -> None:
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ---------- 曲目库 ----------

_SONGS_FILE = Path(config.fortune_songs_file)


def _load_songs() -> list[dict]:
    try:
        return json.loads(_SONGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _pick_todays_song(seed: int) -> dict | None:
    """以日期+QQ号为种子选今日曲目（同人同曲，当日稳定）"""
    songs = _load_songs()
    if not songs:
        return None
    rng = random.Random(seed)
    return rng.choice(songs)


# ---------- 运势生成 ----------

def _stars(n: int) -> str:
    return STAR_FULL * n + STAR_EMPTY * (5 - n)


def _draw_fortune(seed: int) -> dict:
    """确定性生成当日运势数据

    分项规则（主运锚定+旁项波动）：
        打歌运 = 主运，紧贴综合分数（score÷20 四舍五入）
        桃花运 / 事业运 = 主运 ±2 独立浮动（限 0~5）
    """
    rng = random.Random(seed)
    tier_idx = rng.choices(range(len(LUCK_TIERS)), weights=config.fortune_weights, k=1)[0]
    lo, hi = TIER_SCORE_RANGES[tier_idx]
    score = rng.randint(lo, hi - 1)
    main = max(0, min(5, int(score / 20 + 0.5)))

    def _flutter(base: int) -> int:
        return max(0, min(5, base + rng.randint(-2, 2)))

    aspects = {
        "打歌运": main,
        "桃花运": _flutter(main),
        "事业运": _flutter(main),
    }
    return {
        "tier": LUCK_TIERS[tier_idx],
        "tier_idx": tier_idx,
        "score": score,
        "aspects": aspects,
    }


# ---------- 消息构造 ----------

def _fortune_text(uid: str, fortune: dict, escape_bonus: bool) -> str:
    lines = [f"【今日运势】{date.today():%Y-%m-%d}", ""]
    lines.append(f"运势等级：{fortune['tier']}")
    lines.append(f"综合分数：{fortune['score']} 分")
    lines.append("")
    for a in ASPECTS:
        lines.append(f"{a}：{_stars(fortune['aspects'][a])}")
    if escape_bonus:
        lines.append("")
        lines.append(f"✨ {ESCAPE_EVIL_TEXT}")
    return "\n".join(lines)


def _song_text(song: dict) -> str:
    lines = ["", "── 今日曲目 ──"]
    lines.append(song["title"])
    lines.append(f"作曲：{song['artist']}")
    lv = song.get("levels", {})
    parts = []
    # 难度顺序：范式：起源四难度 detected / invaded / massive / reboot
    for diff in ("detected", "invaded", "massive", "reboot"):
        if diff in lv:
            parts.append(f"{diff} {lv[diff]:g}")
    lines.append(" | ".join(parts))
    return "\n".join(lines)


async def _download_cover(filename: str) -> Path | None:
    """下载并缓存曲绘，返回本地路径；失败返回 None（线程池执行避免阻塞事件循环）"""
    cover_dir = Path(config.fortune_cover_dir)
    local = cover_dir / filename
    if local.exists():
        return local
    url = config.fortune_cover_url + urllib.parse.quote(filename)

    def _fetch() -> Path | None:
        try:
            cover_dir.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "NympheaBot/0.1"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            local.write_bytes(data)
            return local
        except Exception:
            return None

    return await asyncio.to_thread(_fetch)


# ---------- 指令 ----------

fortune_cmd = on_command("今日运势", aliases={"抽签", "运势"}, rule=to_me(), priority=5, block=True)


async def _send_fortune(matcher, uid: str, fortune: dict, escape_bonus: bool, prefix: str = ""):
    """构造并发送完整签文（文字 + 今日曲目 + 曲绘），prefix 用于重复抽签时的提示语"""
    seed = int(f"{date.today().strftime('%Y%m%d')}{uid}")
    text = (prefix + "\n\n" if prefix else "") + _fortune_text(uid, fortune, escape_bonus)
    song = _pick_todays_song(seed)
    if song:
        text += "\n" + _song_text(song)
        cover_path = await _download_cover(song["cover"])
        if cover_path:
            file_url = cover_path.resolve().as_uri()
            cover_seg = MessageSegment.image(file_url)
            await matcher.finish(
                Message([MessageSegment.text(text), cover_seg])
            )
    await matcher.finish(text)


@fortune_cmd.handle()
async def handle_fortune(event: MessageEvent):
    uid = str(event.user_id)
    today = date.today()
    data = _load_data()
    users: dict = data.setdefault("users", {})
    user = users.setdefault(uid, {})

    today_key = today.isoformat()
    yesterday_key = (today - timedelta(days=1)).isoformat()

    # 每日一次：重复抽签 → 拒绝文本 + 当日签文重发（防消息淹没后找不到）
    if (existing := user.get(today_key)):
        await _send_fortune(
            fortune_cmd, uid, existing,
            escape_bonus=bool(existing.get("escape_bonus")),
            prefix=AGAINST_GREED_TEXT,
        )

    # 生成今日运势（种子=日期+QQ号，保证当日稳定）
    seed = int(f"{today.strftime('%Y%m%d')}{uid}")
    fortune = _draw_fortune(seed)

    # 脱凶检测：昨日抽过且为凶，今日非凶 → 祝福
    escape_bonus = False
    yd = user.get(yesterday_key)
    if yd and yd.get("tier_idx") in EVIL_TIERS and fortune["tier_idx"] not in EVIL_TIERS:
        escape_bonus = True
    if escape_bonus:
        fortune["escape_bonus"] = True  # 持久化，重复抽签时祝福仍在

    # 记录今日结果
    user[today_key] = fortune
    _save_data(data)

    # 构造并发送
    await _send_fortune(fortune_cmd, uid, fortune, escape_bonus)
