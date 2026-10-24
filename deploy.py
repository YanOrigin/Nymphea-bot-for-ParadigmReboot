# -*- coding: utf-8 -*-
"""统一部署脚本（固定正确顺序，杜绝"重启早于同步"的事故）

顺序（不可颠倒）：
  1. 同步 bot 端 → 私有库 Tracker core
  2. 清两处 __pycache__
  3. 重启生产 bot（计划任务，无 UAC）
  4. 等待监听 + 健康检查 /ping
  5. 校验进程内实际加载的模块源码含指定特征（可选断言）

用法：
  python deploy.py                 # 仅部署
  python deploy.py --expect query_time   # 部署并断言 core.b50_render 源码含 query_time
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
    for f in CORE_FILES:
        src = BOT / "plugins" / "paradigm_score" / f
        if src.exists():
            shutil.copy2(src, TRK / "core" / f)
    step("1) core synced bot -> tracker")


def clear_pycache():
    for d in (BOT / "plugins" / "paradigm_score" / "__pycache__",
              TRK / "core" / "__pycache__"):
        shutil.rmtree(d, ignore_errors=True)
    step("2) pycache cleared")


def clear_plugin_pycache():
    """额外清理 paradigm_score 与 bot_help 的缓存（常改动的插件）"""
    for name in ("paradigm_score", "bot_help"):
        shutil.rmtree(BOT / "plugins" / name / "__pycache__", ignore_errors=True)


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


def health_check(retries: int = 5) -> bool:
    code = r'''
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
    for i in range(retries):
        r = subprocess.run([str(PY), "-c", code], capture_output=True, text=True, timeout=60)
        if "pong" in (r.stdout or ""):
            step(f"4) health OK (attempt {i+1})")
            return True
        time.sleep(8)
    step("4) HEALTH FAILED")
    return False


def verify_loaded(expect: str | None) -> bool:
    """校验「新进程」实际加载的 core 模块源码是否包含期望特征"""
    if not expect:
        return True
    code = rf'''
import sys, inspect
sys.path.insert(0, r"{BOT}")
import nonebot
nonebot.init(driver="~fastapi")
from plugins.paradigm_score.core_loader import get_core
core, src = get_core()
mod = core.b50_render
text = inspect.getsource(mod)
print("SRC:", src)
print("HAS:", "{expect}" in text)
'''
    r = subprocess.run([str(PY), "-c", code], capture_output=True, text=True, timeout=90)
    out = (r.stdout or "") + (r.stderr or "")
    ok = "HAS: True" in out
    src_line = next((ln for ln in out.splitlines() if ln.startswith("SRC:")), "")
    step(f"5) loaded-module check: {'PASS' if ok else 'FAIL'} | {src_line}")
    if not ok:
        print(out[-800:])
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", default=None, help="core.b50_render 源码应包含的特征串")
    ap.add_argument("--wait", type=int, default=22)
    args = ap.parse_args()

    sync_core()
    clear_pycache()
    clear_plugin_pycache()
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
