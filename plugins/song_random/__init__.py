"""打什么：随机推荐一首曲目（纯娱乐向，输出曲目信息 + 曲绘）

指令：
    /打什么       随机抽一首歌
    /打什么 <难度>  限定难度（detected / invaded / massive / reboot）

设计要点：
    - 与 /今日运势 的「今日曲目」同源（同一份 paradigm_songs.json、同一套曲绘缓存）
    - 但**不按日期固定**：每次调用都重新随机，方便反复抽
    - 避开最近抽过的 3 首，减少"连抽同一首"的体感
    - 抽曲历史存 data/song_random_history.json（仅内存态语义，重启也保留）
"""
import asyncio
import json
import random
import urllib.parse
import urllib.request
from pathlib import Path

from nonebot import on_command
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .config import Config, config

__plugin_meta__ = PluginMetadata(
    name="song_random",
    description="打什么（随机推荐范式：起源曲目，含曲绘与四难度定数）",
    usage="/打什么 [难度]\n难度：detected / invaded / massive / reboot",
    type="application",
    homepage=None,
    config=Config,
    supported_adapters={"~onebot.v11"},
)

# ---------- 常量 ----------

DIFF_ORDER = ("detected", "invaded", "massive", "reboot")
DIFF_ALIASES = {
    "d": "detected", "det": "detected", "绿": "detected",
    "i": "invaded", "inv": "invaded", "红": "invaded",
    "m": "massive", "mas": "massive", "紫": "massive",
    "r": "reboot", "reb": "reboot", "橙": "reboot",
}

# ---------- 存储 ----------

# 锚定 bot 根目录（plugins/song_random → 上两级），避免依赖进程 cwd
_BOT_ROOT = Path(__file__).resolve().parent.parent.parent


def _abs(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (_BOT_ROOT / q)


_SONGS_FILE = _abs(config.song_random_songs_file)
_HISTORY_FILE = _abs("data/song_random_history.json")


def _load_songs() -> list[dict]:
    try:
        return json.loads(_SONGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _load_history() -> dict:
    if not _HISTORY_FILE.exists():
        return {}
    try:
        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_history(hist: dict) -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(
            json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass  # 历史记录失败绝不影响抽曲


# ---------- 选曲 ----------

def _parse_diff(text: str) -> tuple[str | None, bool]:
    """解析难度参数 → (难度 key 或 None, 是否非法)"""
    t = text.strip().lower()
    if not t:
        return None, False
    if t in DIFF_ORDER:
        return t, False
    if t in DIFF_ALIASES:
        return DIFF_ALIASES[t], False
    return None, True


def _candidates(songs: list[dict], diff: str | None) -> list[dict]:
    """按条件过滤候选曲目"""
    out = []
    for s in songs:
        levels = s.get("levels") or {}
        if diff:
            if diff not in levels:
                continue
        elif config.song_random_require_massive and "massive" not in levels:
            continue
        if not s.get("cover"):
            continue  # 无曲绘的条目不参与（渲染会缺图）
        out.append(s)
    return out


def _pick(songs: list[dict], uid: str, diff: str | None) -> tuple[dict | None, int]:
    """随机选一首；避开该用户最近抽到的若干首。返回 (曲目, 候选总数)"""
    cands = _candidates(songs, diff)
    if not cands:
        return None, 0

    hist = _load_history()
    recent = hist.get(uid) or []
    avoid_n = max(0, config.song_random_avoid_recent)
    recent_set = set(recent[-avoid_n:]) if avoid_n else set()

    pool = [s for s in cands if s.get("wiki_id") not in recent_set]
    if not pool:
        pool = cands  # 候选太少，放弃规避

    song = random.choice(pool)

    # 记录最近抽到的（去重后保留一定长度）
    wid = song.get("wiki_id")
    if wid:
        seq = [w for w in recent if w != wid]
        seq.append(wid)
        hist[uid] = seq[-max(avoid_n, 20):]
        _save_history(hist)

    return song, len(cands)


# ---------- 曲绘 ----------

async def _download_cover(filename: str) -> Path | None:
    """下载并缓存曲绘（线程池执行，避免阻塞事件循环）"""
    cover_dir = _abs(config.song_random_cover_dir)
    local = cover_dir / filename
    if local.exists():
        return local
    url = config.song_random_cover_url + urllib.parse.quote(filename)

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


# ---------- 文本 ----------

def _song_text(song: dict, diff: str | None, pool_size: int) -> str:
    lv = song.get("levels") or {}
    lines = ["🎵 今天打这个吧", ""]
    lines.append(song.get("title") or "?")
    if song.get("artist"):
        lines.append(f"作曲：{song['artist']}")
    if song.get("wiki_id"):
        lines.append(f"Wiki ID：{song['wiki_id']}")

    # 难度定数：指定难度时高亮该难度
    parts = []
    for d in DIFF_ORDER:
        if d in lv:
            txt = f"{d} {lv[d]:g}"
            parts.append(f"▶{txt}◀" if d == diff else txt)
    if parts:
        lines.append(" | ".join(parts))

    if diff:
        lines.append("")
        lines.append(f"（限定难度：{diff}，候选 {pool_size} 首）")
    return "\n".join(lines)


# ---------- 指令 ----------

random_cmd = on_command("打什么", aliases={"打啥", "来首歌"}, rule=to_me(), priority=5, block=True)


@random_cmd.handle()
async def handle_random(event: MessageEvent, arg: Message = CommandArg()):
    uid = str(event.user_id)
    raw = arg.extract_plain_text().strip()

    diff, bad = _parse_diff(raw)
    if bad:
        await random_cmd.finish(
            "难度只能是：detected / invaded / massive / reboot\n"
            "也可简写 d / i / m / r（或 绿 / 红 / 紫 / 橙）\n"
            "例：/打什么 massive"
        )

    songs = _load_songs()
    if not songs:
        await random_cmd.finish("曲库为空，请稍后再试（或联系 bot 主人刷新曲库）")

    song, pool_size = _pick(songs, uid, diff)
    if not song:
        if diff:
            await random_cmd.finish(f"曲库里没有 {diff} 难度的曲目，换个难度试试？")
        await random_cmd.finish("曲库为空，请稍后再试")

    text = _song_text(song, diff, pool_size)
    cover_path = await _download_cover(song.get("cover") or "")
    if cover_path:
        await random_cmd.finish(
            Message([
                MessageSegment.text(text),
                MessageSegment.image(cover_path.resolve().as_uri()),
            ])
        )
    await random_cmd.finish(text)
