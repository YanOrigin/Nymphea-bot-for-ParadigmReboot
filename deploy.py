# -*- coding: utf-8 -*-
"""统一部署脚本（固定正确顺序，杜绝"重启早于同步"的事故）

顺序（不可颠倒）：
  1. 同步 bot 端 → 私有库 Tracker core
  2. 清两处 __pycache__
  3. 重启生产 bot（计划任务，无 UAC）
  4. 等待监听 + 健康检查 /ping
  5. 校验进程内实际加载的模块源码含指定特征（core 与插件层都查）

用法：
  python deploy.py                      # 仅部署
  python deploy.py --expect query_time  # 部署并断言模块源码含该特征
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

BOT = Path(r"D:\Developed\Nymphea-bot")
TRK = Path(r"D:\Developed\ParadigmReboot-Tracker")
PY = BOT / ".venv" / "Scripts" / "python.exe"
CORE_FILES = ("prd_api.py", "prp_api.py", "b50_render.py")


def step(msg: str):
    print(f"[deploy] {msg}", flush=True)


def sync_core():
    for name in CORE_FILES:
        src = BOT / "plugins" / "paradigm_score" / name
        if src.exists():
            shutil.copy2(src, TRK / "core" / name)
    step("1) core synced bot -> tracker")


def clear_pycache():
    for d in (BOT / "plugins" / "paradigm_score" / "__pycache__",
              BOT / "plugins" / "bot_help" / "__pycache__",
              TRK / "core" / "__pycache__"):
        shutil.rmtree(d, ignore_errors=True)
    step("2) pycache cleared")


def restart_bot(wait: int = 22):
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Stop-ScheduledTask -TaskName 'NympheaBot';"
        "Start-Sleep -Seconds 2;"
        "Get-Process pythonw | Where-Object { $_.Path -like '*Nymphea-bot*' } | Stop-Process -Force;"
        "Start-Sleep -Seconds 2;"
        "Start-ScheduledTask -TaskName 'NympheaBot'"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
    step(f"3) bot restart issued, waiting {wait}s")
    time.sleep(wait)


HEALTH_SCRIPT = '''
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:7999/onebot/v11/ws", open_timeout=8,
                                  additional_headers={"X-Self-ID": "10000"}) as ws:
        replies = []
        async def reader():
            while True:
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed:
                    return
                d = json.loads(raw)
                if "action" in d:
                    a = d.get("action")
                    data = {"user_id": 10000, "nickname": "t"} if a == "get_login_info" else {"message_id": 1}
                    await ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": d.get("echo"), "data": data}))
                    msg = d.get("params", {}).get("message")
                    if isinstance(msg, list):
                        for seg in msg:
                            if seg.get("type") == "text":
                                replies.append(seg["data"]["text"])
        t = asyncio.create_task(reader())
        ev = {"post_type": "message", "message_type": "private", "sub_type": "friend",
              "time": 0, "self_id": 10000, "message_id": 9999001, "user_id": 555,
              "message": [{"type": "text", "data": {"text": "/ping"}}],
              "raw_message": "/ping", "font": 0, "sender": {"user_id": 555, "nickname": "t"}}
        await ws.send(json.dumps(ev))
        await asyncio.sleep(2.5)
        print(replies[0] if replies else "NO_REPLY")
        t.cancel()
asyncio.run(main())
'''

VERIFY_SCRIPT = '''
import sys, inspect
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import nonebot
nonebot.init(driver="~fastapi")
from plugins.paradigm_score.core_loader import get_core
core, src = get_core()
expect = sys.argv[1]
bot = Path(sys.argv[2])
texts = {}
for mod_name in ("b50_render", "prd_api", "prp_api"):
    mod = getattr(core, mod_name, None)
    if mod is not None:
        texts[f"{src}\\{mod_name}"] = inspect.getsource(mod)
plugin_file = bot / "plugins" / "paradigm_score" / "__init__.py"
if plugin_file.exists():
    texts[str(plugin_file)] = plugin_file.read_text(encoding="utf-8")
hit = [loc for loc, tx in texts.items() if expect in tx]
print("SRC:", hit[0] if hit else src)
print("HAS:", bool(hit))
'''


def _run_temp_script(content: str, args: list[str], timeout: int) -> str:
    tmp = Path(BOT) / "_deploy_tmp.py"
    tmp.write_text(content, encoding="utf-8")
    try:
        r = subprocess.run([str(PY), str(tmp), *args],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    finally:
        tmp.unlink(missing_ok=True)


def health_check(retries: int = 5) -> bool:
    for i in range(retries):
        try:
            out = _run_temp_script(HEALTH_SCRIPT, [], 90)
        except subprocess.TimeoutExpired:
            out = ""
        if "pong" in out:
            step(f"4) health OK (attempt {i+1})")
            return True
        time.sleep(8)
    step("4) HEALTH FAILED")
    return False


def verify_loaded(expect: str | None) -> bool:
    if not expect:
        return True
    try:
        out = _run_temp_script(VERIFY_SCRIPT, [expect, str(BOT)], 120)
    except subprocess.TimeoutExpired:
        step("5) loaded-module check: TIMEOUT")
        return False
    ok = "HAS: True" in out
    src_line = next((ln for ln in out.splitlines() if ln.startswith("SRC:")), "")
    step(f"5) loaded-module check: {'PASS' if ok else 'FAIL'} | {src_line}")
    if not ok:
        print(out[-800:])
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", default=None, help="模块源码应包含的特征串")
    ap.add_argument("--wait", type=int, default=22)
    args = ap.parse_args()

    sync_core()
    clear_pycache()
    restart_bot(args.wait)
    ok_health = health_check()
    ok_verify = verify_loaded(args.expect)
    if ok_health and ok_verify:
        step("DEPLOY OK")
        sys.exit(0)
    step("DEPLOY FAILED")
    sys.exit(1)


if __name__ == "__main__":
    main()
