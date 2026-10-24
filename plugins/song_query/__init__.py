"""查歌插件：/查歌 <歌曲名称>

匹配策略（按优先级）：
    1. 精确匹配原曲名（忽略大小写）
    2. 精确匹配别名（data/song_aliases.json）
    3. 模糊匹配原曲名子串（归一化：去空格/忽略大小写/全半角统一）

输出：
    - 唯一命中 → 详情卡片（曲绘 + 曲名 + 曲师 + 四难度定数 + wiki_id）
    - 多个候选 → 列表提示用户用更精确的名字
    - 无命中 → 未找到提示
"""
import asyncio
import json
import re
import unicodedata
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
    name="song_query",
    description="范式：起源查歌（曲名/别名/模糊匹配 → 曲绘 + 四难度定数）",
    usage="/查歌 <歌曲名称>",
    type="application",
    homepage=None,
    config=Config,
    supported_adapters={"~onebot.v11"},
)

# ---------- 数据加载 ----------

_SONGS_FILE = Path(config.song_query_songs_file)
_ALIASES_FILE = Path(config.song_query_aliases_file)


def _load_songs() -> list[dict]:
    try:
        return json.loads(_SONGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _load_aliases() -> dict[str, list[str]]:
    """读别名库：{wiki_id: [别名...]}（与 alias_manager 共用的开源库格式）"""
    try:
        # utf-8-sig 兼容带 BOM 的文件
        raw = json.loads(_ALIASES_FILE.read_text(encoding="utf-8-sig"))
        return {k: v for k, v in raw.items() if k != "_meta"}
    except (json.JSONDecodeError, OSError):
        return {}


# ---------- 归一化与匹配 ----------

def _normalize(s: str) -> str:
    """归一化：NFKC 全半角统一 → 去空格 → 小写"""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _search_songs(query: str) -> tuple[dict | None, list[dict], str | None]:
    """返回 (唯一命中或None, 候选列表, 命中别名或None)"""
    songs = _load_songs()
    nq = _normalize(query)

    # 0) wiki_id 精确（多义别名场景下的精确直达通道）
    for s in songs:
        if s.get("wiki_id", "").lower() == nq:
            return s, [s], None

    # 1) 原曲名精确匹配
    for s in songs:
        if _normalize(s["title"]) == nq:
            return s, [s], None

    # 2) 别名精确匹配（wiki_id 键控库；同名别名可能命中多首）
    aliases = _load_aliases()
    alias_hits: list[dict] = []
    matched_alias: str | None = None
    for wid, alias_list in aliases.items():
        for alias in alias_list:
            if _normalize(alias) == nq:
                matched_alias = alias
                for s in songs:
                    if s.get("wiki_id") == wid:
                        alias_hits.append(s)
    if len(alias_hits) == 1:
        return alias_hits[0], [alias_hits[0]], matched_alias
    if len(alias_hits) > 1:
        # 多义别名：返回候选列表（handler 会渲染序号选择）
        return None, alias_hits[: config.song_query_max_candidates], matched_alias

    # 3) 子串模糊匹配（原曲名）
    candidates = [s for s in songs if nq in _normalize(s["title"])]
    return None, candidates[: config.song_query_max_candidates], None


# ---------- 曲绘 ----------

async def _download_cover(filename: str) -> Path | None:
    cover_dir = Path(config.song_query_cover_dir)
    local = cover_dir / filename
    if local.exists():
        return local
    url = config.song_query_cover_url + urllib.parse.quote(filename)

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


# ---------- 渲染 ----------

_DIFF_ORDER = ("detected", "invaded", "massive", "reboot")


def _song_detail(song: dict, matched_alias: str | None) -> str:
    lines = ["【曲目信息】"]
    lines.append(song["title"])
    lines.append(f"作曲：{song['artist']}")
    if song.get("wiki_id"):
        lines.append(f"Wiki ID：{song['wiki_id']}")
    lv = song.get("levels", {})
    parts = [f"{d} {lv[d]:g}" for d in _DIFF_ORDER if d in lv]
    lines.append(" | ".join(parts))
    if matched_alias:
        lines.append(f"（经别名「{matched_alias}」匹配）")
    return "\n".join(lines)


# ---------- 指令 ----------

song_cmd = on_command("查歌", rule=to_me(), priority=5, block=True)


@song_cmd.handle()
async def handle_query(event: MessageEvent, arg: Message = CommandArg()):
    query = arg.extract_plain_text().strip()
    if not query:
        await song_cmd.finish("用法：/查歌 <歌曲名称|别名|Wiki ID>")

    exact, candidates, matched_alias = _search_songs(query)

    if exact:
        text = _song_detail(exact, matched_alias)
        cover_path = await _download_cover(exact["cover"])
        if cover_path:
            cover_seg = MessageSegment.image(cover_path.resolve().as_uri())
            await song_cmd.finish(Message([MessageSegment.text(text), cover_seg]))
        await song_cmd.finish(text)

    if candidates:
        # 区分"别名多义"与"模糊匹配"两种候选场景
        if matched_alias:
            lines = [f"别名「{matched_alias}」对应多首歌曲，请用 Wiki ID 精确查询：", ""]
            for i, s in enumerate(candidates, 1):
                lv = s.get("levels", {})
                top = max((v for v in lv.values() if v), default=0)
                lines.append(f"{i}. {s['title']}  [{s.get('wiki_id', '')}]（最高定数 {top:g}）")
            lines.append("")
            lines.append("例：/查歌 " + (candidates[0].get("wiki_id") or ""))
        else:
            lines = [f"找到 {len(candidates)} 首可能的歌曲：", ""]
            for i, s in enumerate(candidates, 1):
                lv = s.get("levels", {})
                top = max((v for v in lv.values() if v), default=0)
                lines.append(f"{i}. {s['title']}（最高定数 {top:g}）")
            lines.append("")
            lines.append("请用更完整的曲名重新查询")
        await song_cmd.finish("\n".join(lines))

    await song_cmd.finish(f"未找到与「{query}」匹配的歌曲\n（支持原曲名 / 别名 / Wiki ID 查询）")
