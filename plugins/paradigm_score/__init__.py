"""paradigm_score 查分插件（指令壳层；查分引擎来自 ParadigmReboot-Tracker 私有库）

指令：
    /范式登录            从本机登录工具读取凭据自动绑定游戏账号（推荐，主人用）
    /范式绑定 <凭据>     手动绑定游戏凭据（建议私聊）
    /范式解绑            删除游戏凭据与成绩缓存
    /更新成绩            拉取全量最佳成绩并缓存 + 自动传分 PRP（4h 冷却）
    /绑定prp <用户名> <上传token>   绑定 PRP 账号（查分前置条件）
    /传分                手动触发一次传分（不受冷却限制；幂等覆盖）
    /b50 [模式]          B50 报告图（standard/overflow/global；前缀 f=拟合定数）

冷却规则：
    - 游戏成绩拉取（/更新成绩 与绑定验证）每账号 4 小时一次（防风控封号）
    - 冷却内请求会提示下次可拉取时间；/传分（PRP 侧）与 /b50（只读）不受限

安全规则：
    - 游戏凭据/PRP token 只存本地 data/
    - 任何回复/日志只显示凭据首尾片段
    - 游戏侧只读不写；PRP 传分使用用户自己的 upload_token（这是 PRP 官方支持的用途）
"""
import asyncio
import json
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import on_command
from nonebot.log import logger
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .config import Config, config
from .core_loader import get_core
from . import auth_server

core, _core_src = get_core()  # 查分引擎（私有库优先，内置副本兜底）

__plugin_meta__ = PluginMetadata(
    name="paradigm_score",
    description="范式：起源查分（游戏成绩提取 + PRP 传分 + B50 报告图）",
    usage=(
        "/范式绑定            快爆账号网页授权绑定（点链接登录即可）\n"
        "/范式绑定ios <手机号> <密码>  击弦账号绑定（iOS/外服）\n"
        "/更新成绩            拉取你的最佳成绩列表并自动传分（4h 冷却）\n"
        "/绑定prp <用户名> <上传token>   绑定 PRP 账号\n"
        "/传分                手动传分（不受冷却限制）\n"
        "/b50 [模式]          生成 B50 报告图\n"
        "                     模式：standard(默认)/overflow/global；前缀 f=拟合定数（如 fb50）\n"
        "/范式解绑            删除游戏凭据"
    ),
    type="application",
    homepage=None,
    config=Config,
    supported_adapters={"~onebot.v11"},
)

# ---------- 存储 ----------
# 锚定项目根目录（plugins/paradigm_score → 上两级），避免依赖进程 cwd
_BOT_ROOT = Path(__file__).resolve().parent.parent.parent


def _abs(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (_BOT_ROOT / q)


_TOKEN_FILE = _abs(config.paradigm_token_file)
_SCORES_DIR = _abs(config.paradigm_scores_dir)


def _load_tokens() -> dict:
    if not _TOKEN_FILE.exists():
        return {}
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    # 兼容：过滤旧占位时代存成纯字符串 token 的条目（新格式必须为 dict 凭据）
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if isinstance(v, dict)}
    return data


def _save_tokens(tokens: dict) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")


def _user_score_file(uid: str) -> Path:
    return _SCORES_DIR / f"{uid}.json"


def _mask(creds: dict) -> str:
    """凭据掩码展示"""
    s = creds.get("session", "")
    if s and len(s) >= 8:
        return f"{s[:4]}…{s[-2:]}"
    return "…"


# ---------- 指令 ----------

bind_token = on_command("范式绑定", rule=to_me(), priority=5, block=True)  # 快爆网页授权
bind_ios = on_command("范式绑定ios", aliases={"范式绑定abroad", "范式绑定外服"}, rule=to_me(), priority=5, block=True)
unbind_token = on_command("范式解绑", rule=to_me(), priority=5, block=True)
fetch_scores = on_command("更新成绩", rule=to_me(), priority=5, block=True)
query_score = on_command("b50", rule=to_me(), priority=5, block=True)
ad50_cmd = on_command("ad50", rule=to_me(), priority=5, block=True)
fc50_cmd = on_command("fc50", rule=to_me(), priority=5, block=True)
c50_cmd = on_command("c50", rule=to_me(), priority=5, block=True)
score_cmd = on_command("score", rule=to_me(), priority=5, block=True)


def _mask_device(devid: str) -> str:
    return f"{devid[:8]}…" if devid else "（未固化）"


# ---------- 冷却 ----------

_COOLDOWN_FILE = _SCORES_DIR / "fetch_cooldowns.json"


def _load_cooldowns() -> dict:
    try:
        return json.loads(_COOLDOWN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cooldowns(d: dict) -> None:
    _COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COOLDOWN_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _cooldown_check(uid: str) -> tuple[bool, str]:
    """游戏拉取冷却检查。返回 (允许, 提示语)。"""
    if config.fetch_cooldown <= 0:
        return True, ""
    cds = _load_cooldowns()
    last = float(cds.get(uid, 0))
    remain = last + config.fetch_cooldown - time.time()
    if remain <= 0:
        return True, ""
    nxt = datetime.now() + timedelta(seconds=remain)
    return False, f"⏱️ 成绩拉取冷却中（防频繁登录触发风控）\n下次可拉取时间：{nxt:%m-%d %H:%M}（约 {int(remain/60)+1} 分钟后）"


def _cooldown_mark(uid: str) -> None:
    cds = _load_cooldowns()
    cds[uid] = time.time()
    _save_cooldowns(cds)


# ---------- 公共动作 ----------


async def _action_fetch_and_cache(uid: str, creds: dict) -> list[dict]:
    """拉取游戏成绩 → 缓存 → 打冷却戳。失败抛异常由调用方处理。"""

    def _saver(c: dict):
        tokens = _load_tokens()
        tokens[uid] = c
        _save_tokens(tokens)

    records = await asyncio.to_thread(core.prd_api.fetch_best, creds, _saver)
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(records),
        "records": records,
    }
    _SCORES_DIR.mkdir(parents=True, exist_ok=True)
    _user_score_file(uid).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _cooldown_mark(uid)
    return records


async def _action_push_to_prp(uid: str) -> dict:
    """缓存成绩 → 传分 PRP（幂等）。返回 {ok, reason?/upload, chaotic, unmapped}。"""
    prp = _load_prp().get(uid)
    if not prp:
        return {"ok": False, "reason": "no_prp"}
    sf = _user_score_file(uid)
    if not sf.exists():
        return {"ok": False, "reason": "no_cache"}
    data = json.loads(sf.read_text(encoding="utf-8"))
    records = data.get("records", [])
    if not records:
        return {"ok": False, "reason": "empty_cache"}

    def _do_upload():
        chart_map = _get_chart_map()
        upload, chaotic, unmapped = core.prp_api.build_upload_payload(records, chart_map)
        if not upload:
            return {"upload": [], "chaotic": chaotic, "unmapped": unmapped, "resp": None}
        resp = core.prp_api.upload_records(prp["username"], prp["upload_token"], upload)
        return {"upload": upload, "chaotic": chaotic, "unmapped": unmapped, "resp": resp}

    return {"ok": True, **await asyncio.to_thread(_do_upload)}


def _fmt_push_result(r: dict) -> str:
    lines = [f"✅ 传分完成：成功 {len(r['upload'])} 条"]
    if r["chaotic"]:
        lines.append(f"跳过 chaotic（不计 rating）{len(r['chaotic'])} 条")
    if r["unmapped"]:
        preview = "、".join(r["unmapped"][:5]) + ("…" if len(r["unmapped"]) > 5 else "")
        lines.append(f"⚠️ 未收录谱面 {len(r['unmapped'])} 条：{preview}")
    lines.append("可用 /b50 查看 Rating 报告")
    return "\n".join(lines)


async def _do_bind(matcher, uid: str, creds: dict, source_note: str = ""):
    """公共绑定流：验证 → 首次拉取 → 持久化。绑定验证不受冷却限制（首次使用）。"""
    await matcher.send("⏳ 凭据验证中（首次会拉取成绩）……")

    def _saver(c: dict):
        tokens = _load_tokens()
        tokens[uid] = c
        _save_tokens(tokens)

    try:
        records = await asyncio.to_thread(core.prd_api.fetch_best, creds, _saver)
    except core.prd_api.PrdAPIError as e:
        await matcher.finish(f"❌ 凭据验证失败：{e}\n（未保存无效凭据）")

    tokens = _load_tokens()
    tokens[uid] = creds
    _save_tokens(tokens)

    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(records),
        "records": records,
    }
    _SCORES_DIR.mkdir(parents=True, exist_ok=True)
    _user_score_file(uid).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _cooldown_mark(uid)  # 绑定即拉取，同样计入冷却
    has_cred = "✓ 快爆长期凭据（Session 自动续期）" if creds.get("hykb_credential") else "⚠️ 无长期凭据（Session 过期需重新绑定）"
    await matcher.finish(
        f"✅ 绑定成功（会话 {_mask(creds)} · 设备 {_mask_device(creds.get('deviceid', ''))}）{source_note}\n"
        f"{has_cred}\n"
        f"首次成绩已拉取：{len(records)} 条最佳记录\n"
        f"绑定 PRP 账号后（/绑定prp）成绩会自动上传并生成 B50 报告"
    )


# ---------- 网页授权绑定（快爆：bot 内嵌授权服务器，IPv6 直连） ----------

_auth_server = None  # 懒加载的授权服务器实例


def _get_auth_server():
    global _auth_server
    if _auth_server is None:
        _auth_server = auth_server.AuthServer(port=18964)
        _auth_server.start()
        logger.info(f"授权服务器已启动: {_auth_server.base_url}")
    return _auth_server


@bind_token.handle()
async def handle_bind(event: MessageEvent):
    """快爆网页授权：bot 起本地授权服务器 → 用户手机 IPv6 直连登录 → 自动绑定"""
    uid = str(event.user_id)
    try:
        server = await asyncio.to_thread(_get_auth_server)
    except OSError as e:
        await bind_token.finish(f"❌ 授权服务器启动失败（端口占用？）：{e!r}")

    token = uuid.uuid4().hex[:16]
    server.new_token(token)
    link = f"{server.base_url}/?t={token}"
    await bind_token.send(
        "🔐 快爆账号授权（手机号 + 短信验证码，全程不接触密码）：\n"
        f"{link}\n\n"
        "⏳ 等待你在浏览器中完成登录（10 分钟内有效）……\n"
        "（手机请用流量或与 bot 同网络的 WiFi 打开）"
    )

    deadline = time.time() + 600
    while time.time() < deadline:
        await asyncio.sleep(5)
        r = server.poll(token)
        if r["status"] == "pending":
            continue
        if r["status"] == "expired":
            await bind_token.finish("⌛ 授权链接已失效，请重新 /范式绑定")
        if r["status"] == "done":
            payload = r.get("payload") or {}
            uid_hykb = str(payload.get("userId") or payload.get("uid") or "")
            if not uid_hykb:
                await bind_token.finish("❌ 授权数据异常（缺 uid），请重新 /范式绑定")
            creds = {
                "uid": uid_hykb,
                "hykb_credential": payload,
                "deviceid": uuid.uuid4().hex,  # 每账号终身一个独立设备指纹
            }
            await _do_bind(bind_token, uid, creds,
                           source_note="\n（来源：网页授权，快爆长期凭据已保存）")
    await bind_token.finish("⌛ 授权等待超时（10 分钟），请重新 /范式绑定")


# ---------- 击弦绑定（iOS/外服：手机号+密码） ----------# ---------- 击弦绑定（iOS/外服：手机号+密码） ----------


@bind_ios.handle()
async def handle_bind_ios(event: MessageEvent, arg: Message = CommandArg()):
    """击弦账号绑定：手机号+密码 → 纯协议登录 → 绑定。密码仅用于登录，存本地不回显。"""
    parts = arg.extract_plain_text().split()
    if len(parts) != 2:
        await bind_ios.finish(
            "用法：/范式绑定ios <手机号> <密码>\n"
            "（适用于国服 iOS / 外服账号，即击弦账号体系）\n"
            "⚠️ 建议私聊操作；密码仅存本地用于自动续期，不会出现在任何回复或日志里"
        )
    phone, password = parts[0], parts[1]
    if not (phone.isdigit() and len(phone) == 11):
        await bind_ios.finish("❌ 手机号格式不对（应为 11 位数字）")

    uid = str(event.user_id)
    await bind_ios.send("⏳ 击弦账号登录中……")

    def _saver(c: dict):
        tokens = _load_tokens()
        tokens[uid] = c
        _save_tokens(tokens)

    device_id = str(uuid.uuid4())
    creds = {"jixian": {"phone": phone, "password": password}, "deviceid": device_id}
    try:
        login_res = await asyncio.to_thread(
            core.prd_api.jixian_login, phone, password, device_id)
        creds.update(login_res)
    except core.prd_api.PrdAPIError as e:
        await bind_ios.finish(f"❌ 登录失败：{e}\n（未保存任何信息）")

    # 拉首次成绩验证 + 缓存
    try:
        records = await _action_fetch_and_cache(uid, creds)
    except core.prd_api.PrdAPIError as e:
        await bind_ios.finish(f"❌ 成绩拉取失败：{e}\n（未保存）")

    tokens = _load_tokens()
    tokens[uid] = creds
    _save_tokens(tokens)
    _cooldown_mark(uid)
    await bind_ios.finish(
        f"✅ 击弦账号绑定成功（手机号 {phone[:3]}****{phone[-4:]}）\n"
        f"首次成绩已拉取：{len(records)} 条最佳记录\n"
        f"⚠️ 击弦 Session 有效期较短，失效后 /更新成绩 会自动用保存的账密重新登录\n"
        f"绑定 PRP 账号后（/绑定prp）成绩自动上传并生成 B50 报告"
    )


@unbind_token.handle()
async def handle_unbind(event: MessageEvent):
    uid = str(event.user_id)
    tokens = _load_tokens()
    if uid not in tokens:
        await unbind_token.finish("你还没有绑定过游戏账号")
    tokens.pop(uid)
    _save_tokens(tokens)
    sf = _user_score_file(uid)
    if sf.exists():
        sf.unlink()
    await unbind_token.finish("✅ 已解绑并删除成绩缓存")


@fetch_scores.handle()
async def handle_fetch(event: MessageEvent):
    uid = str(event.user_id)
    creds = _load_tokens().get(uid)
    if not creds:
        await fetch_scores.finish("未绑定游戏账号，请先 /范式绑定（快爆）或 /范式绑定ios（iOS/外服）")

    # 游戏侧 4h 冷却（防风控）
    ok, note = _cooldown_check(uid)
    if not ok:
        await fetch_scores.finish(note)

    await fetch_scores.send("⏳ 正在拉取成绩列表……")
    try:
        records = await _action_fetch_and_cache(uid, creds)
    except core.prd_api.PrdAPIError as e:
        await fetch_scores.finish(f"❌ 拉取失败：{e}")
    except Exception as e:
        await fetch_scores.finish(f"❌ 拉取异常：{e!r}")

    lines = [f"✅ 成绩已更新：{len(records)} 条最佳记录（缓存完成）"]

    # 自动传分（已绑定 PRP 时；幂等覆盖，失败不影响成绩更新）
    if _load_prp().get(uid):
        await fetch_scores.send("⏳ 自动传分到 PRP……")
        try:
            r = await _action_push_to_prp(uid)
            if r.get("ok"):
                lines.append(_fmt_push_result(r))
            else:
                lines.append(f"⚠️ 自动传分跳过（{r.get('reason')}）")
        except core.prp_api.PrpAPIError as e:
            lines.append(f"⚠️ 自动传分失败：{e}")
        except Exception as e:
            logger.warning(f"自动传分异常: {e!r}")
            lines.append(f"⚠️ 自动传分异常，可稍后 /传分 重试")
    else:
        lines.append("💡 绑定 PRP 账号后（/绑定prp）将自动上传成绩")

    await fetch_scores.finish("\n".join(lines))


# ---------- B50 报告 ----------


def _parse_b50_args(raw: str) -> tuple[str, bool]:
    """解析 /b50 参数 → (mode, fitting)。
    模式：standard(默认)/overflow/global；前缀 f = 拟合定数（如 fb50 / fglobal）
    """
    raw = raw.strip().lower().lstrip("/")
    fitting = raw.startswith("f")
    if fitting:
        raw = raw[1:]
    mode = raw if raw in ("standard", "overflow", "global") else "standard"
    return mode, fitting


def _build_judge_map(uid: str) -> dict[tuple[str, str], dict]:
    """从本地游戏缓存建 (wiki_id, difficulty) → 判定数据 映射"""
    cache_path = _user_score_file(uid)
    if not cache_path.exists():
        return {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    game_recs = cache.get("records", [])
    if not game_recs:
        return {}

    _, manual = _load_song_kb()
    slug_to_wid: dict[str, str] = dict(manual)  # slug -> wiki_id（人工映射表）

    out: dict[tuple[str, str], dict] = {}
    for r in game_recs:
        cid = r.get("chart_id") or ""
        parts = cid.split("/")
        if len(parts) != 3:
            continue
        slug, diff = parts[1], parts[2]
        wid = slug_to_wid.get(slug, slug)  # 大部分 slug == wiki_id
        out[(wid, diff)] = {
            "lost": int(r.get("lost_count") or 0),
            "received": int(r.get("received_count") or 0),
            "decrypted": int(r.get("decrypted_count") or 0),
            "decrypted_plus": int(r.get("decrypted_plus_count") or 0),
        }
    return out


def _attach_judge(sections: list[dict], judge_map: dict) -> int:
    """把判定数据附加到 sections 里的记录上，返回成功关联的条数"""
    if not judge_map:
        return 0
    hit = 0
    for sec in sections:
        for rec in sec.get("records", []):
            chart = rec.get("chart") or {}
            key = (chart.get("wiki_id") or "", chart.get("difficulty") or "")
            judge = judge_map.get(key)
            if judge:
                rec["_judge"] = judge
                hit += 1
    return hit


B50_SLOTS = 50   # 总 Rating 的固定分母：Σ50 格 ÷ 50（空格按 0 计）
B35_SLOTS = 35
B15_SLOTS = 15


def _b50_total(records: list[dict], val) -> float:
    """总 Rating = Σ单曲 rating ÷ 50。

    注意：分母恒为 50，**不是**「实际条数」。B15 尚未填满（如只有 2 首）时，
    空位按 0 计——按实际条数平均会把总分虚高（37 条时虚高约 35%）。
    """
    return sum(val(r) for r in records) / B50_SLOTS / 100


def _fetch_b50_sections(username: str, mode: str, fitting: bool) -> dict:
    """按 PRP 前端同款逻辑取数并分节（线程池执行）。
    - standard: scope=b50                     → B15(15)+B35(35)
    - overflow: scope=b50&underflow=5         → B15(15+5)+B35(35+5) 取前15/35
    - global:   scope=best&sort=rating        → 全池前 50 单节
    fitting=True 时 sort_by=fitting_rating 且单元格显示 fitting_rating

    total 恒为 Σ÷50（空格按 0 计）；每节另给 avg（该池实际均值，不含空位）。
    """
    prp = core.prp_api
    sort = "fitting_rating" if fitting else "rating"

    def _val(r):
        return r.get("fitting_rating") if fitting and r.get("fitting_rating") is not None else r.get("rating") or 0

    if mode == "global":
        data = prp.fetch_b50(username)  # scope=best 全量
        recs = data.get("records", [])
        recs.sort(key=lambda x: -_val(x))
        top = recs[:B50_SLOTS]
        avg = sum(_val(r) for r in top) / max(1, len(top)) / 100
        sections = [{"label": "Best 50 (Global)", "avg": avg, "records": top}]
        total = _b50_total(top, _val)
    else:
        params = f"scope=b50&sort_by={sort}&order=desc"
        if mode == "overflow":
            params += "&underflow=5"
        data = prp._get_json(f"{prp.PRP_BASE}/records/{username}?{params}")
        recs = data.get("records", [])
        b35 = sorted([r for r in recs if not (r.get("chart") or {}).get("b15")], key=lambda x: -_val(x))
        b15 = sorted([r for r in recs if (r.get("chart") or {}).get("b15")], key=lambda x: -_val(x))
        top35, top15 = b35[:B35_SLOTS], b15[:B15_SLOTS]
        avg35 = sum(_val(r) for r in top35) / max(1, len(top35)) / 100
        avg15 = sum(_val(r) for r in top15) / max(1, len(top15)) / 100
        total = _b50_total(top35 + top15, _val)
        sections = [
            {"label": "Best 35", "avg": avg35, "records": top35},
            {"label": "Best 15", "avg": avg15, "records": top15},
        ]
    return {"sections": sections, "total": total}


@query_score.handle()
async def handle_b50(event: MessageEvent, arg: Message = CommandArg()):
    mode, fitting = _parse_b50_args(arg.extract_plain_text())
    uid = str(event.user_id)
    prp = _load_prp().get(uid)
    if not prp:
        await query_score.finish(
            "未绑定 PRP 账号。\n"
            "先 /绑定prp <用户名> <上传token>（或传分后自动可用）\n"
            "模式：/b50 standard（默认）· /b50 overflow · /b50 global · 前缀 f=拟合定数（/fb50）"
        )
    mode_label = {"standard": "标准", "overflow": "溢出", "global": "全局"}[mode]
    fit_note = "（拟合定数）" if fitting else ""
    await query_score.send(f"⏳ 正在生成 B50 报告（{mode_label}{fit_note}）……")

    def _do():
        return _fetch_b50_sections(prp["username"], mode, fitting)

    try:
        result = await asyncio.to_thread(_do)
    except PrpAPIError as e:
        await query_score.finish(f"❌ 获取 B50 失败：{e}\n（提示：先 /传分 上传成绩）")
    except Exception as e:
        await query_score.finish(f"❌ 获取异常：{e!r}")

    sections = result["sections"]
    if not sections or all(not s["records"] for s in sections):
        await query_score.finish("PRP 云端没有你的成绩记录，请先 /传分 上传")

    # 关联游戏侧判定数据，让 AD/FC 徽章精确（无缓存时渲染层退回分数推导）
    try:
        judge_map = await asyncio.to_thread(_build_judge_map, uid)
        attached = _attach_judge(sections, judge_map)
        if attached:
            logger.info(f"b50 判定数据关联: {attached} 条")
        else:
            logger.info("b50 无判定数据可关联（游戏缓存缺失或谱面不匹配），徽章按分数推导")
    except Exception as e:
        logger.warning(f"b50 判定关联失败（不影响出图）: {e!r}")

    out = _SCORES_DIR / f"b50_{uid}_{mode}{'_fit' if fitting else ''}.jpg"
    title = f"Paradigm: Reboot B50{' · Fitted' if fitting else ''}"
    try:
        _qt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        img_path = await core.b50_render.render_b50_image(
            prp["username"], prp.get("nickname") or prp["username"],
            sections, result["total"], title, out,
            footer=f"{mode_label}{fit_note} · PRP: {prp['username']}",
            fitting=fitting,
            query_time=_qt,
        )
    except Exception as e:
        logger.exception("b50 render failed")
        await query_score.finish(f"❌ 报告渲染失败：{e!r}")

    from nonebot.adapters.onebot.v11 import MessageSegment
    def _trunc2(x: float) -> str:
        scaled = int(x * 100 + 1e-9)
        return f"{scaled // 100}.{scaled % 100:02d}"

    total_line = f"总 Rating：{_trunc2(result['total'])}"
    await query_score.finish(
        Message([MessageSegment.text(f"📊 {title}\n{total_line}\n"),
                 MessageSegment.image(img_path.resolve().as_uri())])
    )
# ---------- AD50 / FC50（按判定筛选） ----------


def _judge_ok(rec: dict, kind: str) -> bool:
    """判定筛选。kind: 'ad' | 'fc'"""
    lost = int(rec.get("lost_count") or 0)
    if lost != 0:
        return False  # AD 与 FC 都要求 0 Miss
    if kind == "ad":
        received = int(rec.get("received_count") or 0)
        return received == 0  # AD 还要求 0 Good
    return True


def _fetch_judge50(username: str, game_records: list[dict], kind: str) -> dict:
    """按判定筛选游戏成绩 → 对到 PRP chart.id → 取 rating → 排序取前50（线程池执行）"""
    prp = core.prp_api
    chart_map = _get_chart_map()

    # 1) 筛出符合判定的游戏谱面，映射到 PRP chart id
    wanted: dict[int, dict] = {}   # prp_chart_id -> 游戏记录（含判定）
    unmapped = 0
    for r in game_records:
        if not _judge_ok(r, kind):
            continue
        cid = r.get("chart_id")
        if not cid:
            continue
        prp_id = chart_map.resolve(cid)
        if prp_id is None:
            unmapped += 1
            continue
        wanted[prp_id] = r

    if not wanted:
        return {"records": [], "unmapped": unmapped, "total": 0.0, "count": 0}

    # 2) 拉 PRP 全量成绩，按 chart.id 取 rating
    data = prp.fetch_b50(username)
    prp_recs = {int((x.get("chart") or {}).get("id") or 0): x for x in data.get("records", [])}

    merged = []
    for prp_id, game_rec in wanted.items():
        pr = prp_recs.get(prp_id)
        if not pr:
            continue  # PRP 上没有这条成绩（未传分）
        item = dict(pr)
        # 附带判定数据供渲染/调试
        item["_judge"] = {
            "lost": int(game_rec.get("lost_count") or 0),
            "received": int(game_rec.get("received_count") or 0),
            "decrypted": int(game_rec.get("decrypted_count") or 0),
            "decrypted_plus": int(game_rec.get("decrypted_plus_count") or 0),
        }
        merged.append(item)

    merged.sort(key=lambda x: -(x.get("rating") or 0))
    top = merged[:50]
    avg = sum(x.get("rating") or 0 for x in top) / max(1, len(top)) / 100
    return {"records": top, "unmapped": unmapped, "total": avg, "count": len(merged)}


async def _handle_judge50(event: MessageEvent, kind: str):
    uid = str(event.user_id)
    prp = _load_prp().get(uid)
    label = {"ad": "AD50", "fc": "FC50"}[kind]
    judge_desc = {"ad": "All Perfect（0 Good · 0 Miss）", "fc": "Full Combo（0 Miss）"}[kind]

    if not prp:
        await query_score.finish(f"未绑定 PRP 账号，请先 /绑定prp <用户名> <上传token>")

    # 判定数据只在游戏成绩缓存里
    sf = _user_score_file(uid)
    if not sf.exists():
        await query_score.finish(
            f"{label} 需要判定数据，但你还没有成绩缓存。\n"
            f"请先 /更新成绩 拉取一次游戏成绩（之后 {config.fetch_cooldown // 3600} 小时内可直接查）"
        )
    cache = json.loads(sf.read_text(encoding="utf-8"))
    game_records = cache.get("records", [])
    if not game_records:
        await query_score.finish("成绩缓存为空，请先 /更新成绩")

    await query_score.send(f"⏳ 正在统计 {label}（{judge_desc}）……")

    try:
        result = await asyncio.to_thread(_fetch_judge50, prp["username"], game_records, kind)
    except core.prp_api.PrpAPIError as e:
        await query_score.finish(f"❌ 获取 PRP 成绩失败：{e}")
    except Exception as e:
        logger.exception(f"{label} 统计异常")
        await query_score.finish(f"❌ 统计异常：{e!r}")

    recs = result["records"]
    if not recs:
        hint = ""
        if result.get("unmapped"):
            hint = f"\n（有 {result['unmapped']} 个谱面未收录到 PRP 映射）"
        await query_score.finish(
            f"你还没有达成 {judge_desc} 的成绩，或对应成绩尚未传分到 PRP{hint}"
        )

    sections = [{"label": f"{label} · Top {len(recs)}", "avg": result["total"], "records": recs}]
    out = _SCORES_DIR / f"{label.lower()}_{uid}.jpg"
    title = f"Paradigm: Reboot {label}"
    try:
        _qt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        img_path = await core.b50_render.render_b50_image(
            prp["username"], prp.get("nickname") or prp["username"],
            sections, result["total"], title, out,
            footer=f"{judge_desc} · PRP: {prp['username']}",
            fitting=False,
            query_time=_qt,
        )
    except Exception as e:
        logger.exception(f"{label} 渲染失败")
        await query_score.finish(f"❌ 报告渲染失败：{e!r}")

    from nonebot.adapters.onebot.v11 import MessageSegment

    def _trunc2(x: float) -> str:
        scaled = int(x * 100 + 1e-9)
        return f"{scaled // 100}.{scaled % 100:02d}"

    await query_score.finish(
        Message([
            MessageSegment.text(
                f"📊 {title}\n"
                f"达成谱面：{result['count']} 个（展示 rating 前 {len(recs)}）\n"
                f"平均 Rating：{_trunc2(result['total'])}\n"
            ),
            MessageSegment.image(img_path.resolve().as_uri()),
        ])
    )


@ad50_cmd.handle()
async def handle_ad50(event: MessageEvent):
    await _handle_judge50(event, "ad")


@fc50_cmd.handle()
async def handle_fc50(event: MessageEvent):
    await _handle_judge50(event, "fc")


# ---------- C50（冲分候选：离下一档最近的成绩） ----------

# (区间下限, 区间上限, 下一档阈值, 档位名)
# 只收「紧贴下一档、差一点就能升档」的分数段，其余段暂不统计
C50_BANDS: list[tuple[int, int, int, str]] = [
    (1009800, 1009999, 1010000, "MINF+"),
    (998000, 999999, 1000000, "MINF"),
    (988000, 989999, 990000, "MAAA+"),
    (978000, 979999, 980000, "MAAA"),
    (968000, 969999, 970000, "MAA+"),
]
C50_SHOW_LIMIT = 50   # 与 b50 一致的展示上限


def _c50_band(score: int) -> tuple[int, str] | None:
    """命中任一冲分区间则返回 (下一档阈值, 档位名)，否则 None"""
    for lo, hi, nxt, name in C50_BANDS:
        if lo <= score <= hi:
            return nxt, name
    return None


def _c50_bands_text() -> str:
    return " / ".join(f"{lo}~{hi}" for lo, hi, _n, _m in C50_BANDS)


def _fetch_c50(uid: str, prp_username: str | None) -> dict:
    """筛选冲分候选（线程池执行）

    候选 = 本地缓存中落在 C50_BANDS 区间内的谱面，
    按「距下一档还差多少分」升序（差得越少越靠前，越容易提升）。
    """
    cache_path = _user_score_file(uid)
    if not cache_path.exists():
        return {"records": [], "candidates": 0, "band_count": {}, "no_rating": 0, "closest": None}
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    game_recs = cache.get("records", [])

    songs, manual = _load_song_kb()
    by_wid = {s.get("wiki_id"): s for s in songs if s.get("wiki_id")}

    picked: list[dict] = []
    band_count: dict[str, int] = {}
    for r in game_recs:
        score = int(r.get("score") or 0)
        band = _c50_band(score)
        if band is None:
            continue
        nxt, name = band
        band_count[name] = band_count.get(name, 0) + 1
        picked.append({"gap": nxt - score, "rec": r, "next": nxt, "band": name})

    if not picked:
        return {"records": [], "candidates": 0, "band_count": band_count,
                "no_rating": 0, "closest": None}

    # 差距小的优先；同差距时分数高的优先（更值得冲）
    picked.sort(key=lambda x: (x["gap"], -int(x["rec"].get("score") or 0)))
    shown = picked[:C50_SHOW_LIMIT]

    # PRP rating（可选增强）
    # PRP 单次最多返回 200 条且无翻页，按 rating 排序时高分谱面才在里面；
    # C50 候选多为"差一点升档"的中段成绩，故再按 score 抓一次，两次取并集提升覆盖率
    prp_ratings: dict[int, int] = {}
    if prp_username:
        try:
            chart_map = _get_chart_map()
            for sort_by in ("rating", "score"):
                url = (f"{core.prp_api.PRP_BASE}/records/{prp_username}"
                       f"?scope=best&page_size=500&sort_by={sort_by}&order=desc")
                try:
                    data = core.prp_api._get_json(url)
                except Exception:
                    continue
                for x in data.get("records", []):
                    pid = int((x.get("chart") or {}).get("id") or 0)
                    if pid:
                        prp_ratings.setdefault(pid, x.get("rating") or 0)
            for idx, item in enumerate(shown):
                item["_prp_rating"] = prp_ratings.get(
                    chart_map.resolve(item["rec"].get("chart_id") or "") or 0
                )
        except Exception as e:
            logger.warning(f"c50 rating 拉取失败（不影响出图）: {e!r}")

    records, no_rating = [], 0
    for idx, item in enumerate(shown):
        r = item["rec"]
        cid = r.get("chart_id") or ""
        parts = cid.split("/")
        diff = parts[2] if len(parts) == 3 else ""
        slug = parts[1] if len(parts) == 3 else ""
        wid = manual.get(slug, slug)          # 人工映射表修正 slug → wiki_id
        song = by_wid.get(wid) or {}
        rating = item.get("_prp_rating") or 0
        if not rating:
            no_rating += 1
        records.append({
            "score": int(r.get("score") or 0),
            "rating": rating,
            "chart": {
                "id": 0,
                "title": song.get("title") or wid or "?",
                "cover": song.get("cover") or "",
                "difficulty": diff,
                "level": (song.get("levels") or {}).get(diff) or 0,
                "wiki_id": wid,
                "b15": False,
            },
            "_judge": {
                "lost": int(r.get("lost_count") or 0),
                "received": int(r.get("received_count") or 0),
                "decrypted": int(r.get("decrypted_count") or 0),
                "decrypted_plus": int(r.get("decrypted_plus_count") or 0),
            },
            "_gap": item["gap"],
            "_next": item["next"],
            "_band": item["band"],
        })

    return {
        "records": records,
        "candidates": len(picked),
        "band_count": band_count,
        "no_rating": no_rating,
        "closest": shown[0]["gap"] if shown else None,
    }


@c50_cmd.handle()
async def handle_c50(event: MessageEvent):
    uid = str(event.user_id)

    if not _user_score_file(uid).exists():
        await c50_cmd.finish(
            "C50 需要本地成绩缓存。\n"
            f"请先 /更新成绩 拉取一次游戏成绩（之后 {config.fetch_cooldown // 3600} 小时内可直接查）"
        )

    prp = _load_prp().get(uid)
    await c50_cmd.send("⏳ 正在统计冲分候选（离下一档最近的成绩）……")

    try:
        result = await asyncio.to_thread(_fetch_c50, uid, prp["username"] if prp else None)
    except Exception as e:
        logger.exception("c50 统计异常")
        await c50_cmd.finish(f"❌ 统计异常：{e!r}")

    recs = result["records"]
    if not recs:
        await c50_cmd.finish(
            "你目前没有落在冲分区间内的成绩。\n"
            f"（统计范围：{_c50_bands_text()}）"
        )

    sections = [{"label": f"C50 · 冲分候选（{len(recs)}/{result['candidates']}）",
                 "avg": 0.0, "records": recs}]
    out = _SCORES_DIR / f"c50_{uid}.jpg"
    title = "Paradigm: Reboot C50"
    try:
        _qt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        img_path = await core.b50_render.render_b50_image(
            prp["username"] if prp else uid,
            (prp or {}).get("nickname") or (prp or {}).get("username") or uid,
            sections, 0.0, title, out,
            footer=f"距下一档最近的成绩 · {len(recs)} 条",
            fitting=False,
            query_time=_qt,
            hide_total=True,
        )
    except Exception as e:
        logger.exception("c50 渲染失败")
        await c50_cmd.finish(f"❌ 报告渲染失败：{e!r}")

    from nonebot.adapters.onebot.v11 import MessageSegment

    # 区间分布概览（只列有命中的档位）
    dist = " · ".join(
        f"{name} {result['band_count'][name]}"
        for _lo, _hi, _n, name in C50_BANDS
        if result["band_count"].get(name)
    )
    head_lines = [f"📊 {title}", f"候选谱面：{result['candidates']} 个"]
    if result["candidates"] > len(recs):
        head_lines.append(f"（按差距升序展示前 {len(recs)} 个）")
    head_lines.append(f"区间分布：{dist}")
    closest = recs[0]
    head_lines.append(
        f"最接近：{closest['chart']['title']} "
        f"差 {closest['_gap']} 分 → {closest['_band']}"
    )
    if result.get("no_rating"):
        if prp:
            head_lines.append(
                f"（{result['no_rating']} 条未从 PRP 取到 rating，显示 0.00 —— "
                f"该谱面可能未 /传分，或不在 PRP 前 200 条内）"
            )
        else:
            head_lines.append(
                f"（未绑定 PRP，{result['no_rating']} 条 rating 显示 0.00；"
                f"/绑定prp <用户名> <上传token> 后可显示）"
            )

    await c50_cmd.finish(
        Message([
            MessageSegment.text("\n".join(head_lines) + "\n"),
            MessageSegment.image(img_path.resolve().as_uri()),
        ])
    )


# ---------- /score 单曲成绩（本地缓存） ----------

_DIFF_ORDER = ("detected", "invaded", "massive", "reboot")


# 曲库与别名库路径（与 alias_manager / song_query 共享同一批文件）
_SONGS_FILE = _abs("data/paradigm_songs.json")
_ALIASES_FILE = _abs("data/paradigm_aliases.json")


def _load_song_kb() -> tuple[list[dict], dict]:
    """加载曲库与 slug→wiki_id 修正表"""
    try:
        songs = json.loads(_SONGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        songs = []
    manual: dict = {}
    try:
        mp = _abs(config.prp_tables_dir or "data/prp_tables") / "slug_wiki_manual_map.json"
        if mp.exists():
            manual = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return songs, manual


def _resolve_song(query: str) -> tuple[dict | None, list[dict]]:
    """三层解析：wiki_id → 曲名精确 → 别名/模糊（返回候选）"""
    songs, _ = _load_song_kb()
    if not songs:
        return None, []
    nq = "".join(query.split()).lower()

    for s in songs:  # wiki_id
        if (s.get("wiki_id") or "").lower() == nq:
            return s, [s]
    for s in songs:  # 曲名精确
        if "".join(s.get("title", "").split()).lower() == nq:
            return s, [s]

    # 别名
    alias_hits = []
    try:
        db = json.loads(_ALIASES_FILE.read_text(encoding="utf-8-sig"))
        db = {k: v for k, v in db.items() if k != "_meta"}
    except (json.JSONDecodeError, OSError):
        db = {}
    for wid, aliases in db.items():
        if any("".join(a.split()).lower() == nq for a in aliases):
            for s in songs:
                if s.get("wiki_id") == wid:
                    alias_hits.append(s)
                    break
    if len(alias_hits) == 1:
        return alias_hits[0], alias_hits
    if len(alias_hits) > 1:
        return None, alias_hits[:8]

    # 模糊
    cands = [s for s in songs if nq in "".join(s.get("title", "").split()).lower()]
    return None, cands[:8]


def _collect_song_records(uid: str, song: dict) -> tuple[list[dict], list[str]]:
    """从本地缓存取该曲四难度记录。返回 (渲染用记录, 缺少的难度列表)"""
    wid = song.get("wiki_id") or ""
    cache_path = _user_score_file(uid)
    if not cache_path.exists():
        return [], []
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    game_recs = cache.get("records", [])

    _, manual = _load_song_kb()
    # slug：wiki_id 本身，或反查人工映射表
    slugs = {wid}
    for slug, mapped_wid in manual.items():
        if mapped_wid == wid:
            slugs.add(slug)

    by_diff: dict[str, dict] = {}
    for r in game_recs:
        cid = r.get("chart_id") or ""
        parts = cid.split("/")
        if len(parts) != 3:
            continue
        if parts[1] in slugs and parts[2] in _DIFF_ORDER:
            by_diff[parts[2]] = r

    # PRP rating（若已传分）
    ratings: dict[str, int] = {}
    try:
        prp = _load_prp().get(uid)
        if prp:
            data = core.prp_api.fetch_b50(prp["username"])
            for x in data.get("records", []):
                c = x.get("chart") or {}
                if c.get("wiki_id") == wid:
                    ratings[c.get("difficulty") or ""] = x.get("rating") or 0
    except Exception:
        pass

    out, missing = [], []
    no_rating = []
    levels = song.get("levels") or {}
    for diff in _DIFF_ORDER:
        rec = by_diff.get(diff)
        if not rec:
            if diff in levels:
                missing.append(diff)
            continue
        if not ratings.get(diff):
            no_rating.append(diff)
        out.append({
            "score": rec.get("score") or 0,
            "rating": ratings.get(diff, 0),
            "chart": {
                "id": 0,
                "title": song.get("title") or "?",
                "cover": song.get("cover") or "",
                "difficulty": diff,
                "level": levels.get(diff) or 0,
                "b15": False,
            },
        })
    return out, missing, no_rating


@score_cmd.handle()
async def handle_score(event: MessageEvent, arg: Message = CommandArg()):
    query = arg.extract_plain_text().strip()
    if not query:
        await score_cmd.finish("用法：/score <曲名|别名|Wiki ID>\n（数据来自本地成绩缓存，需先 /更新成绩）")

    uid = str(event.user_id)
    cache_path = _user_score_file(uid)
    if not cache_path.exists():
        await score_cmd.finish("还没有成绩缓存，请先 /更新成绩 拉取一次游戏成绩")

    song, cands = _resolve_song(query)
    if not song:
        if cands:
            lines = [f"「{query}」匹配到 {len(cands)} 首，请用 Wiki ID 精确查询：", ""]
            for i, s in enumerate(cands, 1):
                lines.append(f"{i}. {s['title']}  [{s.get('wiki_id', '')}]")
            lines.append("")
            lines.append(f"例：/score {cands[0].get('wiki_id', '')}")
            await score_cmd.finish("\n".join(lines))
        await score_cmd.finish(f"未找到与「{query}」匹配的歌曲")

    await score_cmd.send("⏳ 正在查询单曲成绩……")
    try:
        records, missing, no_rating = await asyncio.to_thread(_collect_song_records, uid, song)
    except Exception as e:
        logger.exception("score 查询失败")
        await score_cmd.finish(f"❌ 查询异常：{e!r}")

    if not records:
        await score_cmd.finish(
            f"你没有「{song['title']}」的游玩记录（本地缓存中未找到）\n"
            f"提示：/更新成绩 可刷新，或确认曲目是否正确"
        )

    sections = [{"label": song["title"], "avg": 0.0, "records": records}]
    out = _SCORES_DIR / f"score_{uid}_{song.get('wiki_id', 'x')}.jpg"
    try:
        _qt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        img_path = await core.b50_render.render_b50_image(
            str(event.user_id), str(event.user_id),
            sections, 0.0, f"Paradigm: Reboot · {song['title']}", out,
            footer=f"单曲成绩 · {song.get('artist') or ''}",
            fitting=False,
            query_time=_qt,
            hide_total=True,
        )
    except Exception as e:
        logger.exception("score 渲染失败")
        await score_cmd.finish(f"❌ 渲染失败：{e!r}")

    from nonebot.adapters.onebot.v11 import MessageSegment
    notes = []
    if missing:
        notes.append(f"未游玩：{'、'.join(missing)}")
    if no_rating:
        notes.append(f"未传分（rating 显示 0.00）：{'、'.join(no_rating)}")
    note = ("\n（" + "；".join(notes) + "）") if notes else ""
    levels = song.get("levels") or {}
    lv_txt = " | ".join(f"{d} {levels[d]:g}" for d in _DIFF_ORDER if d in levels)
    await score_cmd.finish(
        Message([
            MessageSegment.text(
                f"🎵 {song['title']}\n"
                f"{song.get('artist') or ''}\n"
                f"{lv_txt}{note}\n"
            ),
            MessageSegment.image(img_path.resolve().as_uri()),
        ])
    )


_PRP_FILE = _abs(config.prp_accounts_file)


def _load_prp() -> dict:
    """{qq_uid: {"username":..., "upload_token":...}}；过滤旧格式"""
    if not _PRP_FILE.exists():
        return {}
    try:
        data = json.loads(_PRP_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict):
        data = {
            k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("username") and v.get("upload_token")
        }
    return data


def _save_prp(data: dict) -> None:
    _PRP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PRP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_chart_map() -> "core.prp_api.ChartMap":
    tables = _abs(config.prp_tables_dir) if config.prp_tables_dir else None
    return core.prp_api.ChartMap(
        cache_file=_abs(config.prp_chart_cache),
        tables_dir=tables,
    )


def _mask_prp(tok: str) -> str:
    return f"{tok[:4]}…{tok[-2:]}" if len(tok) >= 8 else "…"


bind_prp = on_command("绑定prp", rule=to_me(), priority=5, block=True)
push_scores = on_command("传分", rule=to_me(), priority=5, block=True)


@bind_prp.handle()
async def handle_bind_prp(event: MessageEvent, arg: Message = CommandArg()):
    parts = arg.extract_plain_text().split()
    if len(parts) != 2:
        await bind_prp.finish(
            "用法：/绑定prp <PRP用户名> <上传token>\n"
            "token 获取：prp.icel.site 注册登录 → 个人设置 → 刷新上传令牌\n"
            "⚠️ 建议私聊绑定"
        )
    username, token = parts[0], parts[1]
    await bind_prp.send("⏳ 验证 PRP 账号中……")

    def _verify():
        return core.prp_api.fetch_b50(username)  # 公开只读接口，能拉到即用户名有效

    try:
        data = await asyncio.to_thread(_verify)
    except core.prp_api.PrpAPIError as e:
        await bind_prp.finish(f"❌ PRP 用户名验证失败：{e}")

    accounts = _load_prp()
    accounts[str(event.user_id)] = {"username": username, "upload_token": token}
    _save_prp(accounts)
    n = len(data.get("records", []))
    await bind_prp.finish(
        f"✅ PRP 账号已绑定：{username}（token {_mask_prp(token)}）\n"
        f"当前 PRP 云端已有 {n} 条记录\n"
        f"现在可以 /更新成绩 → /传分"
    )


@push_scores.handle()
async def handle_push(event: MessageEvent):
    """手动传分（不受冷却限制：不触碰游戏服务器，幂等覆盖）。"""
    uid = str(event.user_id)
    if not _load_tokens().get(uid):
        await push_scores.finish("未绑定游戏账号，请先 /范式绑定（快爆）或 /范式绑定ios（iOS/外服）")
    if not _load_prp().get(uid):
        await push_scores.finish("未绑定 PRP 账号，请先 /绑定prp <用户名> <上传token>")

    await push_scores.send("⏳ 正在上传成绩到 PRP……")
    try:
        r = await _action_push_to_prp(uid)
    except core.prp_api.PrpAPIError as e:
        await push_scores.finish(f"❌ 传分失败：{e}")
    except Exception as e:
        await push_scores.finish(f"❌ 传分异常：{e!r}")

    if not r.get("ok"):
        reason = r.get("reason")
        tip = {"no_prp": "未绑定 PRP 账号（/绑定prp）",
               "no_cache": "还没有成绩缓存（/更新成绩）",
               "empty_cache": "成绩缓存为空（/更新成绩）"}.get(reason, reason)
        await push_scores.finish(f"❌ 无法传分：{tip}")
    await push_scores.finish(_fmt_push_result(r))
