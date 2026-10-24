# NapCat 守护启动器
# 作用：启动 NapCatWinBootMain.exe（自动登录指定 QQ 号，见下方 QQ_ACCOUNT）；进程退出后 15 秒重启
# 用法：python napcat_daemon.py   （由任务计划程序在开机时调用）
# 无窗口：CREATE_NO_WINDOW 防止子进程弹出控制台（守护本身由 pythonw 运行）
import subprocess
import time
from datetime import datetime
from pathlib import Path

NAPCAT_DIR = Path(r"D:\Developed\NapCat\NapCat.52230.Shell")
EXE = NAPCAT_DIR / "NapCatWinBootMain.exe"
QQ_ACCOUNT = ""  # 你的 QQ 号（NapCat 快速登录用）
LOG = NAPCAT_DIR / "daemon.log"
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def main() -> None:
    log("napcat daemon start")
    while True:
        log(f"launching {EXE.name} {QQ_ACCOUNT}")
        try:
            proc = subprocess.run(
                [str(EXE), QQ_ACCOUNT],
                cwd=str(NAPCAT_DIR),
                creationflags=CREATE_NO_WINDOW,
            )
            log(f"NapCatWinBootMain exited with code {proc.returncode}")
        except Exception as e:
            log(f"launch error: {e!r}")
        time.sleep(15)


if __name__ == "__main__":
    main()
