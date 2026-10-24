"""别名管理插件：收集曲目别名（本地库，为开源库预热）

指令：
    /别名添加 <wiki_id|曲名|已有别名> <新别名>   一步式（唯一命中直接入库）
    /别名添加 <曲名片段>                          引导式（会话：选序号 → 输别名）
    /别名查询 <曲名|wiki_id|别名>                 查看某曲已有别名（含投票进度）
    /别名投票 <别名>                              为待审核别名投票（3票进入开源库）
    （会话中发送 /别名取消 放弃）

机制：
    - 新别名本地即时生效（查歌立刻可用）
    - 跨歌同名别名允许（玩梗自由）；查歌遇到多义别名列出候选
    - 进入开源库需 3 人投票；种子/历史别名视为已通过
    - 同歌重复拒绝；每人每日限额（默认 20）

别名库格式（data/paradigm_aliases.json）：
    { "<wiki_id>": ["别名1", ...] }  —— wiki_id 键控，即未来开源库格式
投票元数据（data/alias_votes.json，仅本地，不上传）：
    { "approved": ["<wid>::<别名>", ...], "pending": {"<wid>::<别名>": {"voters": [...]}} }
"""
import json
import re
import time
import unicodedata
from datetime import date
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import to_me
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .config import Config, config

__plugin_meta__ = PluginMetadata(
    name="alias_manager",
    description="范式：起源曲目别名收集（wiki_id 锚点 / 会话式交互 / 投票上库）",
    usage=(
        "/别名添加 <wiki_id|曲名|别名> <新别名>\n"
        "/别名添加 <曲名片段>（引导式）\n"
        "/别名查询 <曲名|wiki_id|别名>\n"
        "/别名投票 <别名>\n"
        "会话中发送 /别名取消 放弃"
    ),
    type="application",
    homepage=None,
    config=Config,
    supported_adapters={"~onebot.v11"},
)

# ---------- 数据 ----------

_DB_FILE = Path(config.alias_db_file)
_SONGS_FILE = Path(config.alias_songs_file)
_LIMIT_FILE = _DB_FILE.parent / "alias_daily_count.json"
_VOTES_FILE = Path(config.alias_votes_file)


def _load_db() -> dict[str, list[str]]:
    try:
        # utf-8-sig 兼容带 BOM 的文件（Windows 记事本/PowerShell 写出的常见格式）
        raw = json.loads(_DB_FILE.read_text(encoding="utf-8-sig"))
        return {k: v for k, v in raw.items() if k != "_meta"}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_db(db: dict[str, list[str]]) -> None:
    _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "说明": "范式：起源曲目别名库。键为 wiki_id（稳定锚点），值为别名列表。",
            "格式版本": 1,
        }
    }
    payload.update(db)
    _DB_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_votes() -> dict:
    try:
        data = json.loads(_VOTES_FILE.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        data = {}
    data.setdefault("approved", [])
    data.setdefault("pending", {})

    # 惰性过期清扫：超时未集满的 pending 投票重置（别名本地仍有效，仅清审核进度）
    # 缺 created_at 的旧条目（旧版本写入）视为"现在起算"补时间戳，给完整 TTL 宽限，避免误杀新鲜投票
    if config.alias_vote_ttl > 0 and data["pending"]:
        now = _now_ts()
        expired = []
        for k, entry in data["pending"].items():
            created = entry.get("created_at")
            if created is None:
                entry["created_at"] = now
                continue
            if now - float(created) > config.alias_vote_ttl:
                expired.append(k)
        if expired:
            for k in expired:
                data["pending"].pop(k)
            _save_votes(data)
        elif any("created_at" not in e for e in data["pending"].values()):
            _save_votes(data)  # 回写补上的时间戳
    return data


def _save_votes(votes: dict) -> None:
    _VOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _VOTES_FILE.write_text(
        json.dumps(votes, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_songs() -> list[dict]:
    try:
        return json.loads(_SONGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s).lower()


def _vote_key(wid: str, alias: str) -> str:
    return f"{wid}::{alias}"


def _now_ts() -> float:
    return time.time()


def _alias_display(wid: str) -> str:
    for s in _load_songs():
        if s.get("wiki_id") == wid:
            return s["title"]
    return wid


# ---------- 解析 ----------

def _resolve_key(query: str) -> tuple[dict | None, list[dict]]:
    """三层解析：wiki_id → 原曲名 → 已有别名（单命中）；失败给候选"""
    songs = _load_songs()
    nq = _normalize(query)
    if nq:
        for s in songs:  # 1) wiki_id
            if s.get("wiki_id", "").lower() == nq:
                return s, [s]
        for s in songs:  # 2) 原曲名
            if _normalize(s["title"]) == nq:
                return s, [s]
        db = _load_db()  # 3) 已有别名（取第一首；多义场景由查歌侧处理）
        for wid, aliases in db.items():
            if any(_normalize(a) == nq for a in aliases):
                for s in songs:
                    if s.get("wiki_id") == wid:
                        return s, [s]
        cands = [s for s in songs if nq in _normalize(s["title"])]
        return None, cands[: config.alias_max_candidates]
    return None, []


# ---------- 每日限额 ----------

def _today_count(uid: str) -> int:
    today = date.today().isoformat()
    try:
        e = json.loads(_LIMIT_FILE.read_text(encoding="utf-8-sig")).get(uid)
        if e and e["date"] == today:
            return e["count"]
    except (json.JSONDecodeError, OSError):
        pass
    return 0


def _check_limit(uid: str) -> bool:
    return _today_count(uid) < config.alias_daily_limit


def _bump_limit(uid: str) -> None:
    today = date.today().isoformat()
    try:
        counts = json.loads(_LIMIT_FILE.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        counts = {}
    entry = counts.get(uid, {"date": today, "count": 0})
    if entry["date"] != today:
        entry = {"date": today, "count": 0}
    entry["count"] += 1
    counts[uid] = entry
    _LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LIMIT_FILE.write_text(
        json.dumps(counts, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ---------- 提交 ----------

async def _commit_alias(matcher: Matcher, uid: str, song: dict, new_alias: str):
    new_alias = new_alias.strip()
    if not new_alias or len(new_alias) > 40:
        await matcher.finish("别名无效（空或超过40字符）")
    if not _check_limit(uid):
        await matcher.finish(f"今日添加已达上限（{config.alias_daily_limit} 条/天）")
    wid = song.get("wiki_id")
    if not wid:
        await matcher.finish(f"「{song['title']}」没有 wiki_id，暂不支持添加别名")

    # 仅拒绝同歌重复；跨歌同名允许（玩梗自由，查歌会列候选）
    db = _load_db()
    existing = db.get(wid, [])
    nq = _normalize(new_alias)
    if any(_normalize(a) == nq for a in existing):
        await matcher.finish(f"「{new_alias}」已是「{song['title']}」的别名，无需重复添加")
    cross_song = any(
        _normalize(a) == nq
        for w, al in db.items()
        if w != wid
        for a in al
    )

    db.setdefault(wid, []).append(new_alias)
    _save_db(db)
    _bump_limit(uid)

    # 投票元数据：提交者算首票；种子/历史别名（不在 pending 中）视为已通过
    votes = _load_votes()
    key = _vote_key(wid, new_alias)
    vote_note = ""
    if key not in votes["approved"]:
        entry = votes["pending"].setdefault(key, {"voters": []})
        if uid not in entry["voters"]:
            entry["voters"].append(uid)
        entry.setdefault("created_at", _now_ts())  # 重置后重新计时
        n = len(entry["voters"])
        if n >= config.alias_vote_threshold:
            votes["approved"].append(key)
            votes["pending"].pop(key, None)
            vote_note = f"\n（开源库审核：已集满 {n} 票，将随下次同步上传）"
        else:
            ttl_h = config.alias_vote_ttl // 3600
            ttl_note = f"（{ttl_h} 小时内未集满将重置）" if config.alias_vote_ttl > 0 else ""
            vote_note = (
                f"\n（开源库审核：{n}/{config.alias_vote_threshold} 票{ttl_note}，"
                f"其他人可 /别名投票 {new_alias} 支持它进入开源库）"
            )
        _save_votes(votes)

    cross_note = "\n（注意：该别名已用于其他曲目，/查歌 时会列出候选）" if cross_song else ""
    await matcher.finish(
        f"已添加：「{song['title']}」←「{new_alias}」{cross_note}{vote_note}\n"
        f"（本曲别名数：{len(db[wid])}；今日剩余额度："
        f"{config.alias_daily_limit - _today_count(uid)}）"
    )


# ---------- 指令 ----------

alias_add = on_command("别名添加", rule=to_me(), priority=5, block=True)
alias_query = on_command("别名查询", rule=to_me(), priority=5, block=True)
alias_vote = on_command("别名投票", rule=to_me(), priority=5, block=True)
alias_cancel = on_command("别名取消", rule=to_me(), priority=5, block=True)


@alias_add.handle()
async def handle_add(event: MessageEvent, matcher: Matcher, arg: Message = CommandArg()):
    """分诊：发提示 → 设会话状态 → return；由 got("resp") 静默要下一条消息。"""
    uid = str(event.user_id)
    parts = [p for p in arg.extract_plain_text().split() if p]

    if not parts:
        await matcher.finish(
            "用法：\n"
            "/别名添加 <wiki_id|曲名|已有别名> <新别名>（一步式）\n"
            "/别名添加 <曲名片段>（引导式，多候选时选序号）\n"
            "提示：用 /查歌 可查到每首歌的 Wiki ID"
        )

    if len(parts) >= 2:
        # 一步式：<锚点> <新别名>
        anchor, new_alias = parts[0], " ".join(parts[1:])
        song, cands = _resolve_key(anchor)
        if song:
            await _commit_alias(matcher, uid, song, new_alias)
        if not cands:
            # 锚点完全无匹配：直接报错结束，不能开会话（否则残留会话会吃掉用户后续消息）
            await matcher.finish(f"未找到与「{anchor}」匹配的歌曲，请用 /查歌 确认曲名或 Wiki ID")
        # 锚点多义 → 会话选歌后自动带上别名提交
        matcher.state["_phase"] = "pick"
        matcher.state["_pending_alias"] = " ".join(parts[1:])
        matcher.state["_cands"] = cands
        matcher.state["_anchor"] = anchor
        await matcher.send(_render_candidates(anchor, cands))
        return  # → got("resp") 接收序号

    # 单参数
    anchor = parts[0]
    song, cands = _resolve_key(anchor)
    if song:
        matcher.state["_phase"] = "alias"
        matcher.state["_song"] = song
        await matcher.send(
            f"定位到：{song['title']}（{song.get('wiki_id', '')}）\n"
            "请发送新别名（发送 /别名取消 放弃）"
        )
        return
    if cands:
        matcher.state["_phase"] = "pick"
        matcher.state["_cands"] = cands
        matcher.state["_anchor"] = anchor
        await matcher.send(_render_candidates(anchor, cands))
        return
    await matcher.finish(f"未找到与「{anchor}」匹配的歌曲，请用 /查歌 确认曲名或 Wiki ID")


def _render_candidates(anchor: str, cands: list[dict]) -> str:
    lines = [f"「{anchor}」匹配到 {len(cands)} 首歌，请回复序号：", ""]
    for i, s in enumerate(cands, 1):
        wid = s.get("wiki_id") or "(无wiki_id)"
        lines.append(f"{i}. {s['title']}  [{wid}]")
    lines.append("")
    lines.append("发送 /别名取消 放弃")
    return "\n".join(lines)


@alias_add.got("resp")
async def continue_flow(event: MessageEvent, matcher: Matcher):
    """会话状态机：pick（选歌）↔ alias（收别名）。

    handle_add 已发过提示；got 的 _key_getter 静默要消息，本 handler 直接处理回复。"""
    uid = str(event.user_id)
    state = matcher.state
    phase = state.get("_phase")
    text = str(matcher.get_arg("resp") or "").strip()

    # 会话内取消
    if text.startswith("/别名取消") or text == "别名取消":
        await matcher.finish("已取消本次别名添加")

    if phase == "pick":
        if not text.isdigit():
            await matcher.reject("请回复纯数字序号（或 /别名取消 放弃）")
        cands: list[dict] = state.get("_cands") or []
        idx = int(text) - 1
        if not (0 <= idx < len(cands)):
            await matcher.reject("序号超出范围，请重新回复")
        song = cands[idx]
        pending = state.get("_pending_alias")
        if pending:
            # 一步式锚点多义：选完歌直接提交
            await _commit_alias(matcher, uid, song, str(pending))
        # 引导式：进入收别名阶段（提示随本次 reject 发出）
        state["_phase"] = "alias"
        state["_song"] = song
        await matcher.reject(f"已选择：{song['title']}\n请发送新别名（或 /别名取消 放弃）")

    if phase == "alias":
        song = state.get("_song")
        if not song:
            await matcher.finish("会话状态丢失，请重新 /别名添加")
        if not text or text.startswith("/"):
            await matcher.reject("别名无效，请重新发送（或 /别名取消 放弃）")
        await _commit_alias(matcher, uid, song, text)

    await matcher.finish("会话状态异常，请重新 /别名添加")


@alias_query.handle()
async def handle_query(event: MessageEvent, matcher: Matcher, arg: Message = CommandArg()):
    query = arg.extract_plain_text().strip()
    if not query:
        await matcher.finish("用法：/别名查询 <曲名|wiki_id|别名>")
    song, cands = _resolve_key(query)
    if not song:
        if cands:
            lines = [f"匹配到 {len(cands)} 首，请精确指定："]
            for i, s in enumerate(cands[:5], 1):
                lines.append(f"{i}. {s['title']}")
            await matcher.finish("\n".join(lines))
        await matcher.finish(f"未找到与「{query}」匹配的歌曲")
    wid = song.get("wiki_id", "")
    votes = _load_votes()
    lines = [f"「{song['title']}」（{wid}）的别名："]
    for a in _load_db().get(wid, []):
        key = _vote_key(wid, a)
        if key in votes["pending"]:
            n = len(votes["pending"][key]["voters"])
            lines.append(f"- {a}（开源审核 {n}/{config.alias_vote_threshold}，/别名投票 {a}）")
        else:
            lines.append(f"- {a}")
    await matcher.finish("\n".join(lines))


@alias_vote.handle()
async def handle_vote(event: MessageEvent, matcher: Matcher, arg: Message = CommandArg()):
    uid = str(event.user_id)
    query = arg.extract_plain_text().strip()
    if not query:
        await matcher.finish("用法：/别名投票 <别名>（为待审核别名投上一票）")
    nq = _normalize(query)
    votes = _load_votes()

    matched = [
        (key, entry)
        for key, entry in votes["pending"].items()
        if _normalize(key.split("::", 1)[1]) == nq
    ]
    if not matched:
        approved_hits = [
            k for k in votes["approved"] if _normalize(k.split("::", 1)[1]) == nq
        ]
        if approved_hits:
            await matcher.finish(f"「{query}」已通过投票（在开源库白名单中），无需再投")

        # 重开投票：别名已在本地库但不在白名单（此前投票过期被重置/或种子前旧条目）
        # → 自动重建 pending（当前投票者算首票，重新计时）
        db = _load_db()
        for wid, aliases in db.items():
            hit = next((a for a in aliases if _normalize(a) == nq), None)
            if hit is None:
                continue
            key = _vote_key(wid, hit)
            votes["pending"][key] = {
                "voters": [uid],
                "created_at": _now_ts(),
            }
            _save_votes(votes)
            title = _alias_display(wid)
            ttl_h = config.alias_vote_ttl // 3600
            ttl_note = f"（{ttl_h} 小时内未集满将再次重置）" if config.alias_vote_ttl > 0 else ""
            await matcher.finish(
                f"重新开启投票：「{title}」←「{hit}」（1/{config.alias_vote_threshold} 票）{ttl_note}\n"
                f"邀请其他玩家 /别名投票 {hit} 吧"
            )
        # 未命中本地库
        await matcher.finish(f"没有找到待投票的别名「{query}」")

    results = []
    for key, entry in matched:
        wid, alias = key.split("::", 1)
        title = _alias_display(wid)
        if uid in entry["voters"]:
            results.append(
                f"「{title}」←「{alias}」：你已投过票（{len(entry['voters'])}/{config.alias_vote_threshold}）"
            )
            continue
        entry["voters"].append(uid)
        n = len(entry["voters"])
        if n >= config.alias_vote_threshold:
            votes["approved"].append(key)
            votes["pending"].pop(key, None)
            results.append(f"「{title}」←「{alias}」：集满 {n} 票，已进入开源库白名单 🎉")
        else:
            results.append(f"「{title}」←「{alias}」：{n}/{config.alias_vote_threshold} 票")

    _save_votes(votes)
    await matcher.finish("\n".join(results))


@alias_cancel.handle()
async def handle_cancel():
    await alias_cancel.finish(
        "已取消进行中的添加（如有会话）。\n如需新增别名：/别名添加 <锚点> <别名>"
    )
