"""alias_sync：把社区投票通过的别名库自动同步到 GitHub 开源库

设计要点：
- 无任何 QQ 指令（防止群内恶意刷指令），后台定时任务自动执行
- 同步唯一依据：alias_votes.json 的 approved 白名单
- 导出内容确定性（无时间戳字段）：内容没变就不产生空 commit
- 远端若被人为改动（含本地没有的条目）→ 拒绝覆盖，仅告警日志，不报给群友
- alias_deleted.json 墓碑：本地 /别名删除 掉的白名单条目不算"远端篡改"，
  会随下次同步从远端移除，避免删除后又被推回
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

import nonebot
from nonebot.log import logger

from .config import config

try:
    from .github_client import GitHubClient, GitHubError
except ImportError:  # pragma: no cover
    from plugins.alias_sync.github_client import GitHubClient, GitHubError  # type: no-redef

# ---------- 路径 ----------

ROOT = Path.cwd()
VOTES_FILE = ROOT / config.alias_votes_file
STATE_FILE = ROOT / "data" / "alias_sync_state.json"
TOMB_FILE = ROOT / "data" / "alias_deleted.json"

META_NOTE = "范式：起源曲目别名库。键为 wiki_id（稳定锚点），值为别名列表。"


# ---------- JSON 小工具 ----------


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning(f"alias_sync 读 {path.name} 失败: {e!r}")
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fingerprint(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------- 白名单导出（确定性：相同白名单 → 相同字节） ----------


def _load_tomb() -> set[str]:
    """墓碑：被 /别名删除 主动删掉的白名单条目"""
    data = _read_json(TOMB_FILE, {"deleted": []})
    if not isinstance(data, dict):
        return set()
    return {x for x in data.get("deleted", []) if isinstance(x, str)}


def _approved_pairs() -> dict[str, list[str]]:
    votes = _read_json(VOTES_FILE, {"approved": [], "pending": {}})
    tomb = _load_tomb()
    pairs: dict[str, set] = {}
    for item in votes.get("approved", []):
        if "::" not in item:
            continue
        if item in tomb:
            continue  # 已被删除：不再导出
        wid, alias = item.split("::", 1)
        if wid and alias:
            pairs.setdefault(wid, set()).add(alias)
    return {wid: sorted(pairs[wid]) for wid in sorted(pairs)}


def export_for_github() -> str:
    """导出内容与 _meta 沿用本地开源库格式；键序确定，无时间戳。"""
    pairs = _approved_pairs()
    payload = {
        "_meta": {
            "说明": META_NOTE,
            "格式版本": 1,
        },
        **pairs,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _parse_remote_pairs(content: str) -> dict[str, list[str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, list)}


# ---------- 核心同步 ----------


def sync_to_github() -> dict:
    """执行一次同步，返回 {ok, action, message}。action: create/update/unchanged/conflict"""
    if not config.github_repo or not config.github_token:
        return {"ok": False, "action": "error", "message": "未配置 ALIAS_SYNC_GITHUB_REPO / ALIAS_SYNC_GITHUB_TOKEN"}

    client = GitHubClient(
        repo=config.github_repo,
        token=config.github_token,
        branch=config.github_branch,
        timeout=config.github_timeout,
    )

    approved_pairs = _approved_pairs()
    exported = export_for_github()
    n_songs = len(approved_pairs)
    n_aliases = sum(len(v) for v in approved_pairs.values())

    state = _read_json(STATE_FILE, {})

    # 1. 读远端
    try:
        remote_content, remote_sha = client.get_file(config.github_path)
    except GitHubError as e:
        return {"ok": False, "action": "error", "message": f"读取远端失败：{e}"}

    # 2. 无变化 → 跳过（不产生空 commit）
    if remote_content is not None and remote_content == exported:
        state["last_sync_check"] = _now()
        _write_json(STATE_FILE, state)
        return {
            "ok": True,
            "action": "unchanged",
            "message": f"远端已是最新（{n_songs} 首 · {n_aliases} 条别名）",
        }

    # 3. 决定动作与 commit message
    if remote_content is None:
        action = "create"
        commit_msg = "feat: 初始化社区曲目别名库（Nymphea-bot 同步）"
    else:
        remote_pairs = _parse_remote_pairs(remote_content)
        remote_set = {f"{w}::{a}" for w, al in remote_pairs.items() for a in al}
        local_set = {f"{w}::{a}" for w, al in approved_pairs.items() for a in al}
        tomb = _load_tomb()
        # 远端有但本地没有的条目：墓碑中的属于"我们主动删的"，其余才是外部改动
        extra_all = remote_set - local_set
        extra_tomb = extra_all & tomb
        extra = extra_all - tomb
        if extra:
            logger.warning(
                f"alias_sync 冲突中止：远端含本地没有的 {len(extra)} 条 "
                f"（{', '.join(sorted(extra)[:5])}{'…' if len(extra) > 5 else ''}），已拒绝覆盖"
            )
            return {
                "ok": False,
                "action": "conflict",
                "message": f"远端含本地没有的 {len(extra)} 条别名，拒绝覆盖（详见日志）",
            }
        action = "update"
        if extra_tomb:
            commit_msg = (
                f"update: 移除 {len(extra_tomb)} 条已删别名，同步 "
                f"{n_songs} 首 / {n_aliases} 条（Nymphea-bot）"
            )
        else:
            commit_msg = f"update: 同步 {n_songs} 首 / {n_aliases} 条别名（Nymphea-bot）"

    # 4. 提交
    try:
        commit_sha = client.put_file(config.github_path, exported, commit_msg, remote_sha)
    except GitHubError as e:
        if "422" in str(e):
            return {"ok": False, "action": "conflict", "message": f"提交冲突（远端刚被更新，下轮重试）：{e}"}
        return {"ok": False, "action": "error", "message": f"提交失败：{e}"}

    # 5. 记录状态
    state["last_pushed_fingerprint"] = _fingerprint(exported)
    state["last_commit_sha"] = commit_sha
    state["last_sync_time"] = _now()
    _write_json(STATE_FILE, state)

    verb = {"create": "初始化创建", "update": "更新"}[action]
    return {
        "ok": True,
        "action": action,
        "message": (
            f"已{verb} GitHub 别名库（{config.github_repo}/{config.github_path}@{config.github_branch}）"
            f"{n_songs} 首 / {n_aliases} 条，commit {commit_sha[:7]}"
        ),
    }


# ---------- 后台自动同步（无 QQ 指令） ----------


async def _auto_loop():
    interval = max(60, config.auto_sync_interval)
    logger.info(f"alias_sync 自动同步已启动，间隔 {interval}s")
    await asyncio.sleep(45)  # 启动缓冲，避开与其他模块抢网络
    while True:
        try:
            r = await asyncio.to_thread(sync_to_github)
            if r.get("ok") and r.get("action") == "unchanged":
                logger.debug(f"alias_sync: {r['message']}")
            elif r.get("ok"):
                logger.info(f"alias_sync 自动同步: {r['message']}")
            else:
                logger.warning(f"alias_sync 自动同步失败: {r.get('message', '')[:200]}")
        except Exception:
            logger.exception("alias_sync 自动同步异常")
        await asyncio.sleep(interval)


driver = nonebot.get_driver()


@driver.on_startup
async def _start_auto_sync():
    if config.auto_sync_interval > 0:
        asyncio.create_task(_auto_loop())
    else:
        logger.info("alias_sync 自动同步未启用（ALIAS_SYNC_INTERVAL=0）")
