# NapCat 守护启动器
# 作用：启动 NapCatWinBootMain.exe（自动登录指定 QQ 号，见下方 QQ_ACCOUNT）；进程退出后 15 秒重启
# 用法：python napcat_daemon.py   （由任务计划程序在开机时调用）
# 无窗口：CREATE_NO_WINDOW 防止子进程弹出控制台（守护本身由 pythonw 运行）
#
# 同时兼任「连接看门狗」：主循环每 60s 检查一次 NapCat 是否与 bot 建立连接，
# 连续 3 次异常就重启 NapCat（解决系统睡眠唤醒后 NapCat 僵死、永不重连的问题）。
# 之所以放在这里而不是独立任务：本机 UAC 未提权，无法注册新的计划任务；
# 而本守护本来就随 NapCatDaemon 任务开机自启，挂在这里同样能长期常驻。
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

NAPCAT_DIR = Path(r"D:\Developed\NapCat\NapCat.52230.Shell")
EXE = NAPCAT_DIR / "NapCatWinBootMain.exe"
QQ_ACCOUNT = ""  # 你的 QQ 号（NapCat 快速登录用）
LOG = NAPCAT_DIR / "daemon.log"
CREATE_NO_WINDOW = 0x08000000

# ---- 看门狗参数 ----
BOT_PORT = 7999
CHECK_INTERVAL = 60      # 检查间隔（秒）
FAIL_THRESHOLD = 3       # 连续几次异常才动手
WAKE_GRACE = 180         # 检测到刚睡醒 / 刚重启时的额外宽限
SLEEP_DRIFT = 90         # 墙钟间隔超过 检查间隔+此值 → 认为经历了睡眠

_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()


def log(msg: str) -> None:
    """写日志；utf-8 编码。文件超过 20MB 时轮转为 .old（避免无限膨胀）。"""
    try:
        if LOG.exists() and LOG.stat().st_size > 20 * 1024 * 1024:
            old = LOG.with_suffix(".log.old")
            try:
                if old.exists():
                    old.unlink()
                LOG.rename(old)
            except OSError:
                pass
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


# ---------- 连接探测（纯标准库） ----------

def _bot_listening(port: int = BOT_PORT, timeout: float = 2.0) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _napcat_connected(port: int = BOT_PORT) -> bool:
    """是否有客户端与 bot 建立了 ESTABLISHED 连接。

    中文 Windows 的 netstat 输出是 GBK，必须取 bytes 再容错解码
    （text=True 默认 UTF-8 会抛 UnicodeDecodeError，stdout 变 None）。
    """
    try:
        raw = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
        ).stdout or b""
    except Exception:
        return True  # 探测失败保守当正常，绝不因探测失败去杀进程

    out = raw.decode("utf-8", "replace")
    if "ESTABLISHED" not in out:
        out = raw.decode("gbk", "replace")

    needle = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        local, remote, state = parts[1], parts[2], parts[3].upper()
        if state != "ESTABLISHED":
            continue
        if local.endswith(needle) or local.rsplit(":", 1)[-1] == str(port):
            return True
        if remote.endswith(needle):
            return True
    return False


# ---------- 重启 NapCat ----------

def _kill_qq_tree() -> None:
    """杀掉 QQ / NapCatWinBootMain，否则新实例会因单例检测直接退出"""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-Process NapCatWinBootMain,QQ -ErrorAction SilentlyContinue | Stop-Process -Force;"
        "Start-Sleep -Seconds 3"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        log(f"kill_qq_tree 失败: {e!r}")


def _restart_napcat(reason: str) -> None:
    global _proc
    log(f"看门狗触发重启 NapCat —— {reason}")
    with _proc_lock:
        _kill_qq_tree()
        try:
            if _proc is not None and _proc.poll() is None:
                _proc.terminate()
                try:
                    _proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _proc.kill()
        except Exception as e:
            log(f"终止旧 NapCat 失败: {e!r}")
        _proc = None   # 主循环发现为 None 会重新拉起


# ---------- 看门狗线程 ----------

def _watchdog_loop() -> None:
    log("看门狗已启动（检查 NapCat ↔ bot 连接）")
    conn_fail = 0
    listen_fail = 0
    last_wall = time.time()
    grace_until = 0.0
    time.sleep(CHECK_INTERVAL)   # 启动宽限，避免与主循环抢启动

    while True:
        now = time.time()
        # 睡眠检测
        if now - last_wall > CHECK_INTERVAL + SLEEP_DRIFT:
            gap = int(now - last_wall)
            log(f"检测到睡眠/挂起（墙钟间隔 {gap}s），给 {WAKE_GRACE}s 宽限期")
            grace_until = now + WAKE_GRACE
            conn_fail = listen_fail = 0
        last_wall = now

        if now < grace_until:
            time.sleep(CHECK_INTERVAL)
            continue

        # 宽限期结束的瞬间，重新开始计数（避免宽限前的失败次数被沿用）
        if grace_until and now >= grace_until:
            grace_until = 0.0
            listen_fail = conn_fail = 0

        if not _bot_listening():
            listen_fail += 1
            conn_fail = 0
            log(f"bot 未监听 {BOT_PORT}（{listen_fail}/{FAIL_THRESHOLD}）")
            if listen_fail >= FAIL_THRESHOLD:
                # bot 没起来时重启 NapCat 没意义；这里只记日志，等 bot 侧自愈
                log("bot 长时间未监听：等待 NympheaBot 任务自行拉起（看门狗不介入）")
                listen_fail = 0
                grace_until = time.time() + WAKE_GRACE
        else:
            listen_fail = 0
            if not _napcat_connected():
                conn_fail += 1
                log(f"NapCat 未连接（{conn_fail}/{FAIL_THRESHOLD}）")
                if conn_fail >= FAIL_THRESHOLD:
                    _restart_napcat(f"连续 {conn_fail} 次未连接")
                    conn_fail = 0
                    grace_until = time.time() + WAKE_GRACE
            else:
                if conn_fail:
                    log("NapCat 连接已恢复")
                conn_fail = 0

        time.sleep(CHECK_INTERVAL)


def main() -> None:
    global _proc
    log("napcat daemon start")
    threading.Thread(target=_watchdog_loop, daemon=True, name="conn-watchdog").start()

    while True:
        log(f"launching {EXE.name} {QQ_ACCOUNT}")
        try:
            with _proc_lock:
                _proc = subprocess.Popen(
                    [str(EXE), QQ_ACCOUNT],
                    cwd=str(NAPCAT_DIR),
                    creationflags=CREATE_NO_WINDOW,
                )
            proc = _proc
            rc = proc.wait()          # 阻塞直到 NapCat 退出（或被看门狗 terminate）
            log(f"NapCatWinBootMain exited with code {rc}")
        except Exception as e:
            log(f"launch error: {e!r}")
        time.sleep(15)


if __name__ == "__main__":
    main()
