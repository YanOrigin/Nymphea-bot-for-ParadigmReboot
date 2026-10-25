"""PRP 曲目元数据缓存（定数/曲名/曲绘/难度等）

版本更新后手动刷新一次:
    python -m core.song_meta --refresh

缓存: tables/prp_songs_cache.json
    {"_meta": {...}, "songs": {"s/<wiki_id>/<difficulty>": {...}}}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_TABLES = Path(__file__).resolve().parent.parent / "tables"
_CACHE_FILE = _TABLES / "prp_songs_cache.json"
_BASE = "https://api.prp.icel.site/api/v2"

_CACHE = None


def load() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        _CACHE = json.loads(_CACHE_FILE.read_text(encoding="utf-8")).get("songs") or {}
    except (OSError, json.JSONDecodeError):
        _CACHE = {}
    return _CACHE


def get(chart_id: str) -> dict | None:
    return load().get(chart_id)


def get_constant(chart_id: str) -> float | None:
    m = get(chart_id)
    if not m:
        return None
    v = m.get("constant")
    return float(v) if isinstance(v, (int, float)) else None


def enrich(chart_id: str, fallback_title: str | None = None) -> dict:
    m = get(chart_id) or {}
    parts = chart_id.split("/")
    slug = parts[1] if len(parts) > 1 else chart_id
    return {
        "title": m.get("title") or fallback_title or slug,
        "artist": m.get("artist") or "",
        "constant": m.get("constant"),
        "cover": m.get("cover") or "",
        "bpm": m.get("bpm") or "",
        "difficulty": m.get("difficulty") or (parts[2] if len(parts) > 2 else ""),
    }


def refresh(timeout: int = 60) -> dict:
    import urllib.request
    req = urllib.request.Request(f"{_BASE}/songs",
                                headers={"User-Agent": "NympheaBot/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        songs = json.loads(r.read().decode("utf-8"))
    out = {}
    for x in songs:
        wid, diff = x.get("wiki_id"), x.get("difficulty")
        if not wid or not diff:
            continue
        out[f"s/{wid}/{diff}"] = {
            "title": x.get("title") or "",
            "artist": x.get("artist") or "",
            "constant": x.get("level"),
            "cover": x.get("cover") or "",
            "bpm": x.get("bpm") or "",
            "difficulty": diff,
        }
    payload = {"_meta": {"fetched_at": int(time.time()), "count": len(out)},
               "songs": out}
    _TABLES.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    global _CACHE
    _CACHE = out
    return payload["_meta"]


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        meta = refresh()
        print(f"已刷新 {meta['count']} 条 → {_CACHE_FILE}")
    else:
        print(f"缓存: {len(load())} 条, 用 --refresh 刷新")
