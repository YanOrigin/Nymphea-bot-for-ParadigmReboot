"""范式：起源 国服 API 客户端（移植自 paradigm-reboot-data 工具集 PrdClient）

协议要点（详见原仓库 README）：
- AES-128-CBC 加密通道：请求/响应均为 IV(16B) + 密文；key = KEY ^ OTP（编译期常量）
- 认证三件套：X-Session + X-UID + Authorization: Bearer <session>
- 成绩端点：GET /server/play/get_best → {"status":"OK","data":[...每谱面最佳成绩...]}
- 响应头 X-New-Session 会轮换会话，需要回写持久化（否则旧会话会逐渐失效）
- 只读不写：绝不调用 upload_result / update_save 等写端点（原仓库铁律）
"""
import base64
import json
import os
import urllib.request
import urllib.error
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 内置传输密钥（游戏 4.12 编译期常量，与账号/会话无关；来源 tools/gamekeys.py）
# 传输密钥属于 ParadigmReboot-Tracker 私有库（core/prd_api.py）。
# 此文件是 bot 内的兜底副本，不含密钥——core_loader 会优先加载私有库。
_KEY_HEX = ""
_OTP_HEX = ""
_KEY_MISSING = True
_KEY_EFF = bytes(16)


def _require_key():
    """密钥缺失：开源副本无游戏密钥，快速失败引导安装 Tracker 私有库"""
    raise PrdAPIError(
        "查分引擎未安装：游戏传输密钥属于私有库 ParadigmReboot-Tracker。\n"
        "请部署 Tracker（默认探测 D:\\Developed\\ParadigmReboot-Tracker，或设 TRACKER_CORE_DIR）"
    )


GAME_BASE = "https://api.paradigmchina.tunergames.com"
GAME_VERSION, GAME_BUILD = "4.12", "1481"


class PrdAPIError(Exception):
    """游戏 API 调用失败"""


class PrdClient:
    """单个用户的游戏 API 会话客户端

    绑定凭据两种形态（bot 统一存为 creds dict）：
      - credential 形态：{"hykb_credential": {...}}   → 可静默换 Session（推荐，长期有效）
      - session 形态：{"session": "...", "uid": "..."} → 直接可用，过期需重新绑定
    """

    def __init__(self, creds: dict, device_id: str | None = None, creds_saver=None,
                 base_url: str | None = None, platform: str | None = None):
        self.creds = creds
        self.creds_saver = creds_saver  # 回调(dict)：session 轮换时持久化
        self.session = creds.get("session") or ""
        self.uid = str(creds.get("uid") or "")
        self.rvh = creds.get("rvh", "")
        self.device_id = device_id or creds.get("deviceid") or str(uuid.uuid4())
        # 击弦(iOS)走 TUNER 端点 + iOS 头; 快爆(安卓)走 GAME_BASE
        self.is_jixian = self.uid.startswith("jx_")
        self.base = base_url or (TUNER_BASE if self.is_jixian else GAME_BASE)
        self.platform = platform or ("iOS" if self.is_jixian else "Android")
        if not self.session or not self.uid:
            raise PrdAPIError("凭据缺少 session/uid")

    # ---------- AES 通道 ----------

    def _enc(self, obj) -> bytes:
        payload = json.dumps(obj).encode()
        pad = 16 - len(payload) % 16
        payload += bytes([pad]) * pad
        iv = os.urandom(16)
        e = Cipher(algorithms.AES(_KEY_EFF), modes.CBC(iv)).encryptor()
        return iv + e.update(payload) + e.finalize()

    def _dec(self, data: bytes):
        if not data or len(data) <= 16:
            return None
        iv, ct = data[:16], data[16:]
        if len(ct) % 16:
            return None
        d = Cipher(algorithms.AES(_KEY_EFF), modes.CBC(iv)).decryptor()
        pt = d.update(ct) + d.finalize()
        if pt and 0 < pt[-1] <= 16:
            pt = pt[:-pt[-1]]
        return pt

    # ---------- 请求 ----------

    def call(self, path: str, method: str = "GET", body=None, timeout: int = 30):
        _require_key()
        headers = {
            "Content-Type": "application/octet-stream", "User-Agent": "BestHTTP",
            "X-Session": self.session,
            "X-Api-Version": "1", "X-api-version": "1",
            "X-game-version": GAME_VERSION, "X-game-build": GAME_BUILD,
            "X-platform": self.platform, "X-Language": "zh",
            "X-Device-Id": self.device_id, "X-Data-Version": self.rvh,
        }
        if not self.is_jixian:
            # 快爆链需要 X-UID + Bearer; 击弦链不需要(原版实测)
            headers["X-UID"] = self.uid
            headers["Authorization"] = "Bearer " + self.session
        data = self._enc(body) if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raw = e.read()
            dec = self._dec(raw) if raw and len(raw) > 16 else None
            msg = dec.decode("utf-8", "replace")[:200] if dec else f"HTTP {e.code}"
            raise PrdAPIError(msg) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PrdAPIError(f"网络错误: {e!r}") from e
        raw = r.read()
        # 会话轮换：服务器通过响应头下发新 session，必须回写持久化
        ns = r.headers.get("X-New-Session")
        dv = r.headers.get("X-Data-Version")
        if ns:
            self.session = ns
        if dv:
            self.rvh = dv
        if (ns or dv) and self.creds_saver:
            self.creds["session"] = self.session
            self.creds["rvh"] = self.rvh
            self.creds_saver(self.creds)
        pt = self._dec(raw)
        if pt is None:
            raise PrdAPIError("响应解密失败（可能游戏大版本更新导致密钥变更）")
        try:
            return json.loads(pt)
        except json.JSONDecodeError as e:
            raise PrdAPIError(f"解密后非 JSON: {e}") from e


# ---------- 快爆 credential → Session（静默续期，移植自 fetch_scores_hykb.py 层B） ----------

GAME_ID = "24931"


TUNER_BASE = "https://tuner-api.paradigmchina.tunergames.com"


def jixian_login(phone: str, password: str, device_id: str, timeout: int = 30) -> dict:
    """击弦账号（国服 iOS/外服）登录链：手机号+密码 → authToken → 游戏Session。
    成功返回 {"session", "uid"}；uid 用手机号合成（击弦链无独立数字 uid）。"""
    body = json.dumps({"phone": phone, "password": password}).encode()
    r = urllib.request.Request(TUNER_BASE + "/account/login", data=body,
        headers={"Content-Type": "application/json;charset=utf-8",
                 "User-Agent": "BestHTTP", "X-Device-Id": device_id}, method="POST")
    try:
        auth = json.loads(urllib.request.urlopen(r, timeout=timeout).read())["data"]["authToken"]
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
        detail = ""
        if isinstance(e, urllib.error.HTTPError):
            detail = e.read().decode("utf-8", "replace")[:120]
        raise PrdAPIError(f"击弦登录失败: {e!r} {detail}") from e

    r2 = urllib.request.Request(TUNER_BASE + "/account/refresh_token", data=b"{}",
        headers={"Content-Type": "application/json;charset=utf-8", "User-Agent": "BestHTTP",
                 "X-Device-Id": device_id, "X-Session": auth}, method="POST")
    try:
        session = json.loads(urllib.request.urlopen(r2, timeout=timeout).read())["data"]["authToken"]
    except Exception as e:
        raise PrdAPIError(f"击弦 Session 换取失败: {e!r}") from e
    return {"session": session, "uid": f"jx_{phone}"}


def hykb_refresh_session(credential: dict, device_id: str, timeout: int = 15) -> dict:
    """用快爆 credential 静默换新 Session。成功返回 {"session","uid"}，失败抛 PrdAPIError"""
    uid = str(credential.get("userId") or credential.get("uid") or "")
    token = credential.get("token") or ""
    login_type = str(credential.get("loginType", 1))
    if not uid or not token:
        raise PrdAPIError("credential 缺少 userId/token")

    body = json.dumps({"c": "authgame", "a": "getAccessToken", "v": "1546",
                       "app_id": GAME_ID, "uid": uid,
                       "type": login_type, "user_token": token}).encode()
    r = urllib.request.Request("https://api.3839app.com/kuaibao/android/devsdk.php",
        data=body, headers={"Content-Type": "application/json;charset=utf-8",
                            "User-Agent": "okhttp/3.12.0"}, method="POST")
    try:
        d = json.loads(urllib.request.urlopen(r, timeout=timeout).read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        raise PrdAPIError(f"快爆 devsdk 请求失败: {e!r}") from e
    at = (d.get("result") or {}).get("access_token")
    if not at:
        raise PrdAPIError("快爆 credential 已失效（devsdk 未返回 access_token）")

    body2 = json.dumps({"uid": int(uid), "access_token": at}).encode()
    r2 = urllib.request.Request(GAME_BASE + "/account/login",
        data=body2, headers={"Content-Type": "application/json;charset=utf-8",
                             "User-Agent": "BestHTTP", "X-Device-Id": device_id}, method="POST")
    try:
        d2 = json.loads(urllib.request.urlopen(r2, timeout=timeout).read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        raise PrdAPIError(f"游戏登录请求失败: {e!r}") from e
    session = d2.get("session")
    if not session:
        raise PrdAPIError(f"游戏登录失败: {str(d2)[:120]}")
    return {"session": session, "uid": uid, "deviceid": device_id}


# ---------- 高层操作 ----------


def _renew_session(creds: dict, device_id: str, timeout: int = 30) -> dict:
    """按凭据类型静默续期 session。

    - 击弦（iOS）：creds["jixian"] = {"phone","password"} → 手机号密码重新登录
    - 快爆（安卓）：creds["hykb_credential"] → devsdk 换 access_token 再登录
    两者都没有则抛 PrdAPIError（需要用户重新绑定）。
    """
    jx = creds.get("jixian")
    if jx and jx.get("phone") and jx.get("password"):
        return jixian_login(jx["phone"], jx["password"], device_id, timeout=timeout)
    cred = creds.get("hykb_credential")
    if cred:
        return hykb_refresh_session(cred, device_id, timeout=timeout)
    raise PrdAPIError("凭据已过期且无可用于自动续期的信息（请重新绑定账号）")


def fetch_best(creds: dict, creds_saver=None, timeout: int = 30) -> list[dict]:
    """拉取全量成绩（每谱面最佳）。自动处理 session 轮换与静默续期。

    设备指纹策略（防风控）：
    - 每个账号一个稳定 device_id：首次生成后立即持久化到该用户凭据
    - 此后所有请求（含静默续期）都用同一个 device_id，模拟真实玩家的稳定设备
    - 严禁多账号共享 device_id（collaborator 警告：会被击弦网络封杀）

    凭据形态：支持有 session 的（直接用）与 credential-only 的（先静默换 session）。
    续期来源：击弦用 jixian 手机号密码重登；快爆用 hykb_credential 换 token。
    """
    # device_id 终身固化：在任何请求前确定并持久化
    device_id = creds.get("deviceid") or str(uuid.uuid4())
    if creds.get("deviceid") != device_id:
        creds["deviceid"] = device_id
        if creds_saver:
            creds_saver(creds)

    # session 缺失：按类型静默换新
    if not creds.get("session"):
        creds.update(_renew_session(creds, device_id, timeout=timeout))
        if creds_saver:
            creds_saver(creds)

    client = PrdClient(creds, device_id=device_id, creds_saver=creds_saver)

    # 会话预热：新换的 session 首调重端点偶发 500/400（服务端异步开户），
    # 先打轻端点 /server/user/login 既能确认登录态又能稳定会话
    # (击弦链无需预热: 原版实测直接 get_best 即可)
    if not client.is_jixian:
        try:
            client.call("/server/user/login", timeout=timeout)
        except PrdAPIError:
            pass  # 预热失败不致命，主调用失败还有续期重试兜底

    try:
        data = client.call("/server/play/get_best", timeout=timeout)
    except PrdAPIError:
        # Session 失效（401 等）→ 按凭据类型静默续期后重试一次（沿用同一 device_id）
        try:
            new = _renew_session(creds, device_id, timeout=timeout)
        except PrdAPIError:
            raise  # 无法自愈：把原始调用错误抛给上层
        creds.update(new)
        if creds_saver:
            creds_saver(creds)
        client = PrdClient(creds, device_id=device_id, creds_saver=creds_saver)
        data = client.call("/server/play/get_best", timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise PrdAPIError(f"get_best 返回结构异常: {str(data)[:120]}")
    return data["data"]


# token 字段兼容：/范式绑定 支持两种格式（base64 的 creds JSON 或纯 session）
# 约定：绑定时输入 base64(json) 或 "session|uid" 简写


def parse_bind_token(raw: str) -> dict:
    """解析用户绑定的凭据。支持:
    1) base64(JSON) —— 完整 creds（含 hykb_credential 最佳）
    2) session|uid  —— 简写
    """
    raw = raw.strip()
    # 尝试 base64 → JSON
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        obj = json.loads(decoded)
        if isinstance(obj, dict) and (obj.get("session") or obj.get("hykb_credential")):
            if obj.get("hykb_credential") and not obj.get("session"):
                # 只有 credential → 先换一次 session
                dev = obj.get("deviceid") or str(uuid.uuid4())
                obj.update(hykb_refresh_session(obj["hykb_credential"], dev))
                obj.setdefault("deviceid", dev)
            obj.setdefault("uid", str((obj.get("hykb_credential") or {}).get("userId", "")))
            if not obj.get("uid"):
                raise PrdAPIError("凭据缺少 uid")
            return obj
    except PrdAPIError:
        raise
    except Exception:
        pass
    # 简写 session|uid
    if "|" in raw:
        s, _, u = raw.partition("|")
        s, u = s.strip(), u.strip()
        if s and u:
            return {"session": s, "uid": u}
    raise PrdAPIError(
        "无法识别的凭据格式。支持: base64(creds JSON) 或 session|uid 简写"
    )


# ---------- 自动化凭据获取（登录工具输出文件 → bot 凭据） ----------


def _find_tool_data_dirs() -> list[Path]:
    """登录工具的数据目录候选（按优先级）

    标准部署：D:\\Developed\\paradigm-reboot-data\\data（bot 的同级目录）
    支持环境变量 PARADIGM_TOOL_DATA 覆盖
    """
    here = Path(__file__).parent  # plugins/paradigm_score
    develop = here.parent.parent.parent  # D:\Developed（bot 根的上级）
    candidates = []
    env = os.getenv("PARADIGM_TOOL_DATA", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(develop / "paradigm-reboot-data" / "data")  # 标准部署位
    candidates.append(here.parent.parent / "paradigm-reboot-data" / "data")  # bot 内部署位
    return candidates


def read_local_creds() -> dict:
    """从本地登录工具的数据目录读取最新凭据，转为 bot creds。

    返回 {creds, source_file}；找不到/无效时抛 PrdAPIError。
    支持的来源（按优先级）：
      1. hykb_credential.json —— 快爆长期凭据（bot 用它静默换 session，长期有效）
      2. creds_weblogin.json —— 快爆网页授权产物（含 session+uid，可能已过期）
    两者合并为完整凭据；device_id 缺失时生成一个并写回工具目录（账号终身绑定）。
    """
    last_err = ""
    for data_dir in _find_tool_data_dirs():
        weblogin = data_dir / "creds_weblogin.json"
        credential = data_dir / "hykb_credential.json"
        if not weblogin.exists() and not credential.exists():
            last_err = f"未找到凭据文件（找过 {data_dir}）"
            continue

        creds: dict = {"deviceid": ""}
        # 1) 长期 credential（hykb）——核心凭据，bot 靠它自动续期
        if credential.exists():
            try:
                c = json.loads(credential.read_text(encoding="utf-8"))
                if c.get("userId") or c.get("uid"):
                    creds["hykb_credential"] = c
                    creds["uid"] = str(c.get("userId") or c.get("uid"))
            except (json.JSONDecodeError, OSError) as e:
                last_err = f"hykb_credential.json 读取失败: {e!r}"

        # 2) weblogin 的 session（若有，可能已过期但可先试）
        if weblogin.exists():
            try:
                w = json.loads(weblogin.read_text(encoding="utf-8"))
                creds.setdefault("session", w.get("session") or "")
                creds.setdefault("uid", str(w.get("uid") or ""))
                creds["deviceid"] = creds.get("deviceid") or w.get("deviceid") or ""
                creds.setdefault("rvh", w.get("rvh", ""))
            except (json.JSONDecodeError, OSError) as e:
                last_err = f"creds_weblogin.json 读取失败: {e!r}"

        if not creds.get("hykb_credential") and not creds.get("session"):
            last_err = f"{data_dir} 中凭据文件均为空"
            continue
        if not creds.get("uid"):
            last_err = f"{data_dir} 中凭据缺少 UID"
            continue

        # 3) device_id：缺则生成并写回工具目录（该账号终身用这个）
        if not creds.get("deviceid"):
            dev_file = data_dir / ".device_id"
            if dev_file.exists():
                creds["deviceid"] = dev_file.read_text(encoding="utf-8").strip()
            if not creds.get("deviceid"):
                creds["deviceid"] = str(uuid.uuid4())
                try:
                    data_dir.mkdir(parents=True, exist_ok=True)
                    (data_dir / ".device_id_bot").write_text(creds["deviceid"], encoding="utf-8")
                except OSError:
                    pass  # 写不进也不影响（凭据里已带）
        return {"creds": creds, "source": str(data_dir)}

    raise PrdAPIError(last_err or "未找到登录工具的数据目录")
