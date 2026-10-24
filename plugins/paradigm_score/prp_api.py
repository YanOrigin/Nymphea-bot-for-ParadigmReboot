"""PRP（PRProber，prp.icel.site）API 客户端 —— 零框架依赖的独立核心模块

职责：成绩上传（传分）与 B50 拉取。 Rating 由 PRP 服务器计算（与游戏内零差，原仓库已验证）。

游戏 chart_id（"s/<slug>/<difficulty>"）→ PRP 内部 chart_id 的三层解析：
    1. PRP /songs 直映射：s/<wiki_id>/<difficulty>（1326 条，动态刷新）
    2. 原表 276 条（prp_chart_id_map.json，覆盖直映射未收录的）
    3. slug 人工修正表 24 条（slug_wiki_manual_map.json，如 zangsongge→sousouka）

注意：
- upload_token 是 PRP 账号的"上传令牌"（prp.icel.site 个人设置获取），与游戏凭据无关
- is_replace=True：覆盖模式（低分可覆盖高分、幂等可重传）
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

PRP_BASE = "https://api.prp.icel.site/api/v2"
UA = "NympheaBot/0.1"

DIFF_ORDER = ("detected", "invaded", "massive", "reboot")


class PrpAPIError(Exception):
    """PRP API 调用失败"""


# ---------- HTTP ----------


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        try:
            j = json.loads(body)
            msg = j.get("message") or j.get("error") or body
        except json.JSONDecodeError:
            msg = body
        raise PrpAPIError(f"HTTP {e.code}: {msg}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PrpAPIError(f"网络错误: {e!r}") from e
    except json.JSONDecodeError as e:
        raise PrpAPIError(f"返回非 JSON: {e}") from e


def _post_json(url: str, payload: dict, timeout: int = 60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        try:
            j = json.loads(body)
            msg = j.get("message") or j.get("error") or body
        except json.JSONDecodeError:
            msg = body
        raise PrpAPIError(f"HTTP {e.code}: {msg}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PrpAPIError(f"网络错误: {e!r}") from e
    except json.JSONDecodeError as e:
        raise PrpAPIError(f"返回非 JSON: {e}") from e


# ---------- chart_id 三层解析 ----------


class ChartMap:
    """游戏 chart_id → PRP chart_id 解析器（带缓存与刷新）"""

    def __init__(self, cache_file: Path, tables_dir: Optional[Path] = None,
                 refresh_interval: int = 24 * 3600):
        self.cache_file = cache_file
        self.refresh_interval = refresh_interval
        # 静态表（可选；拆库时随包带走）
        self._static_map: dict = {}
        self._slug_manual: dict = {}
        if tables_dir:
            for name in ("prp_chart_id_map.json",):
                p = tables_dir / name
                if p.exists():
                    try:
                        self._static_map = json.loads(p.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        pass
            p = tables_dir / "slug_wiki_manual_map.json"
            if p.exists():
                try:
                    self._slug_manual = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
        self._auto: dict = {}
        self._loaded_at = 0.0
        self._load_cache()

    # -- 缓存 --

    def _load_cache(self):
        try:
            d = json.loads(self.cache_file.read_text(encoding="utf-8"))
            self._auto = d.get("auto", {})
            self._loaded_at = float(d.get("loaded_at", 0))
        except (json.JSONDecodeError, OSError, ValueError):
            self._auto, self._loaded_at = {}, 0.0

    def _save_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps({
                "auto": self._auto, "loaded_at": self._loaded_at,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    # -- 刷新 --

    def ensure(self, force: bool = False):
        """缓存缺失或过期时从 PRP /songs 重建直映射"""
        if not force and self._auto and (time.time() - self._loaded_at) < self.refresh_interval:
            return
        songs = _get_json(f"{PRP_BASE}/songs", timeout=60)
        auto = {}
        for x in songs:
            wid, diff, cid = x.get("wiki_id"), x.get("difficulty"), x.get("id")
            if wid and diff and cid:
                auto[f"s/{wid}/{diff}"] = cid
        if not auto:
            raise PrpAPIError("PRP /songs 返回空数据")
        self._auto = auto
        self._loaded_at = time.time()
        self._save_cache()

    # -- 解析 --

    def resolve(self, game_chart_id: str) -> Optional[int]:
        """游戏 chart_id → PRP chart_id；三层解析，失败返回 None"""
        self.ensure()
        hit = self._auto.get(game_chart_id)
        if hit is not None:
            return hit
        hit = self._static_map.get(game_chart_id)
        if hit is not None:
            return hit
        # 第三层：slug 人工修正（s/<slug>/<difficulty> → wiki_id 替换后重查直映射）
        parts = game_chart_id.split("/")
        if len(parts) == 3:
            slug, diff = parts[1], parts[2]
            wid = self._slug_manual.get(slug)
            if wid:
                return self._auto.get(f"s/{wid}/{diff}")
        return None

    def resolve_batch(self, game_chart_ids: list) -> tuple[list[dict], list[str]]:
        """批量解析。返回 ([{game_id, prp_id, score}], [未解析的游戏id])"""
        self.ensure()
        ok, missing = [], []
        for gid in game_chart_ids:
            prp_id = self.resolve(gid)
            if prp_id is None:
                missing.append(gid)
            else:
                ok.append(prp_id)
        return ok, missing


# ---------- 高层操作 ----------


def upload_records(username: str, upload_token: str,
                   chart_score_pairs: list[dict]) -> dict:
    """上传成绩到 PRP。

    chart_score_pairs: [{"chart_id": <PRP int id>, "score": <游戏分数>}]
    is_replace=True：覆盖模式（幂等、可重传）
    返回 PRP 响应（HTTP 201，每条含服务器计算的 rating）
    """
    if not username or not upload_token:
        raise PrpAPIError("缺少 PRP 用户名或上传 token")
    body = {
        "upload_token": upload_token,
        "is_replace": True,
        "play_records": chart_score_pairs,
    }
    return _post_json(f"{PRP_BASE}/records/{username}", body)


def fetch_b50(username: str, timeout: int = 30) -> dict:
    """拉取 PRP 已算好的成绩全量（按 rating 降序）。B35/B15 划分见 chart.b15 标记。"""
    return _get_json(
        f"{PRP_BASE}/records/{username}?scope=best&page_size=500&sort_by=rating&order=desc",
        timeout=timeout,
    )


def build_upload_payload(game_records: list[dict], chart_map: ChartMap) -> tuple[list[dict], list[str], list[str]]:
    """游戏成绩 → PRP 上传载荷。

    返回 (upload_items, skipped_chaotic, unmapped)
    - chaotic 谱不计入 rating（PRP 无收录），自动跳过
    """
    upload, chaotic, unmapped = [], [], []
    for r in game_records:
        cid = r.get("chart_id")
        score = r.get("score")
        if not cid or score is None:
            continue
        parts = cid.split("/")
        if len(parts) == 3 and parts[2] == "chaotic":
            chaotic.append(cid)
            continue
        prp_id = chart_map.resolve(cid)
        if prp_id is None:
            unmapped.append(cid)
            continue
        upload.append({"chart_id": prp_id, "score": int(score)})
    return upload, chaotic, unmapped
