"""song_refresh：曲库每日自动更新

游戏版本更新出新歌/改定数后，自动从 PRP 聚合 API 拉取并重写 data/paradigm_songs.json，
查歌 / 今日运势 / 别名系统无需重启即用上新数据。
设计要点：
- 每日 HH:MM 定时 + 可选启动即刷（SONG_REFRESH_ON_START）
- 聚合逻辑与既有曲库快照一致：API 扁平难度行 → 按 song_id 聚合 → 消费格式
- 内容无变化不重写文件（保留 mtime，也方便人工核对 diff）
- 原子写入（临时文件 + os.replace），杜绝写一半被读坏
- 曲绘不做预下载：查歌/运势本来就是按需下载缓存，新歌首次被抽到时自然落盘
"""
import asyncio
import json
import os
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import nonebot
from nonebot.log import logger

from .config import config

ROOT = Path.cwd()
SONGS_FILE = ROOT / config.songs_file

DIFF_ORDER = {"detected": 0, "invaded": 1, "massive": 2, "reboot": 3}


def _read_current() -> list:
    try:
        with open(SONGS_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"song_refresh 读曲库失败: {e!r}")
        return []


def _write_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _fetch_and_aggregate() -> list[dict]:
    """拉 API → 按 song_id 聚合成消费格式。失败抛异常由调用方兜底。"""
    req = urllib.request.Request(
        config.api_url,
        headers={"User-Agent": "nymphea-bot", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=config.request_timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"API 返回结构异常：{type(raw).__name__}")

    songs: dict[int, dict] = {}
    for row in raw:
        sid = row.get("song_id")
        if sid is None:
            continue
        s = songs.setdefault(
            sid,
            {
                "title": row.get("title") or "",
                "artist": row.get("artist") or "",
                "cover": row.get("cover") or "",
                "wiki_id": row.get("wiki_id") or "",
                "levels": {},
            },
        )
        diff = row.get("difficulty")
        lvl = row.get("level")
        if diff in DIFF_ORDER and isinstance(lvl, (int, float)):
            s["levels"][diff] = lvl

    result = []
    for sid in sorted(songs):
        s = songs[sid]
        if not s["levels"]:
            continue  # 没有任何有效难度的脏数据行，不入库
        s["levels"] = {
            d: s["levels"][d]
            for d in DIFF_ORDER
            if d in s["levels"]
        }
        result.append(s)
    return result


def refresh_songs() -> dict:
    """拉取并按需更新曲库文件。返回 {ok, changed, old, new, added, message}"""
    try:
        fresh = _fetch_and_aggregate()
    except Exception as e:
        return {"ok": False, "changed": False, "message": f"拉取曲库失败：{e!r}"}

    if not fresh:
        return {"ok": False, "changed": False, "message": "API 返回空数据，跳过更新"}

    old = _read_current()

    def _key(s: dict):
        return json.dumps(s, ensure_ascii=False, sort_keys=True)

    old_map = {s.get("wiki_id"): s for s in old if isinstance(s, dict)}
    new_map = {s.get("wiki_id"): s for s in fresh}
    added = sorted(set(new_map) - set(old_map))
    removed = sorted(set(old_map) - set(new_map))
    changed_consts = sorted(
        w for w in (set(old_map) & set(new_map))
        if old_map[w].get("levels") != new_map[w].get("levels")
    )
    changed_meta = sorted(
        w for w in (set(old_map) & set(new_map))
        if any(old_map[w].get(k) != new_map[w].get(k) for k in ("title", "artist", "cover"))
    )

    is_same = [_key(s) for s in old] == [_key(s) for s in fresh]
    if is_same:
        return {
            "ok": True,
            "changed": False,
            "old": len(old),
            "new": len(fresh),
            "added": [],
            "message": f"曲库无变化（{len(fresh)} 首）",
        }

    _write_atomic(SONGS_FILE, fresh)
    parts = [f"{len(old)} → {len(fresh)} 首"]
    if added:
        parts.append(f"新增 {len(added)}")
    if removed:
        parts.append(f"移除 {len(removed)}")
    if changed_consts:
        parts.append(f"定数变动 {len(changed_consts)}")
    if changed_meta:
        parts.append(f"信息变动 {len(changed_meta)}")
    return {
        "ok": True,
        "changed": True,
        "old": len(old),
        "new": len(fresh),
        "added": added,
        "message": "，".join(parts),
    }


# ---------- 调度 ----------


def _seconds_until(hhmm: str) -> float:
    h, m = hhmm.split(":")
    now = datetime.now()
    target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _daily_loop():
    logger.info(f"song_refresh 每日刷新已启动，时刻 {config.refresh_time}")
    while True:
        delay = _seconds_until(config.refresh_time)
        logger.debug(f"song_refresh 下次刷新在 {delay/3600:.1f} 小时后")
        await asyncio.sleep(delay)
        try:
            r = await asyncio.to_thread(refresh_songs)
            if not r.get("ok"):
                logger.warning(f"song_refresh: {r.get('message', '')[:200]}")
            elif r.get("changed"):
                added = r.get("added") or []
                extra = f"，新曲目：{('、'.join(added[:8]) + ('…' if len(added) > 8 else ''))}" if added else ""
                logger.info(f"song_refresh 曲库已更新: {r['message']}{extra}")
            else:
                logger.debug(f"song_refresh: {r['message']}")
        except Exception:
            logger.exception("song_refresh 刷新异常")


driver = nonebot.get_driver()


@driver.on_startup
async def _start_daily_refresh():
    if config.refresh_on_start:
        asyncio.create_task(_startup_refresh())
    if config.refresh_time:
        asyncio.create_task(_daily_loop())
    else:
        logger.info("song_refresh 定时刷新未启用（SONG_REFRESH_TIME 为空）")


async def _startup_refresh():
    await asyncio.sleep(10)  # 启动缓冲，避开开机网络未就绪
    try:
        r = await asyncio.to_thread(refresh_songs)
        if r.get("ok") and r.get("changed"):
            logger.info(f"song_refresh 启动刷新: {r['message']}")
        elif r.get("ok"):
            logger.debug(f"song_refresh 启动刷新: {r['message']}")
        else:
            logger.warning(f"song_refresh 启动刷新失败: {r.get('message', '')[:200]}")
    except Exception:
        logger.exception("song_refresh 启动刷新异常")
