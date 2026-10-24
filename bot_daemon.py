# Nymphea-bot 守护启动器
# 作用：启动 bot.py；进程退出后 10 秒自动重启（崩溃拉起）；自身日志追加到 logs\daemon.log，
#       bot 的 stdout/stderr 重定向到 logs\bot.log（计划任务下无控制台，必须显式重定向）
# 用法：python bot_daemon.py   （由任务计划程序在开机时调用）
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
PY = ROOT / ".venv" / "Scripts" / "pythonw.exe"  # 无控制台版解释器
LOG = ROOT / "logs" / "daemon.log"
BOT_LOG = ROOT / "logs" / "bot.log"
CREATE_NO_WINDOW = 0x08000000  # 双保险：即使误用 python.exe 也不弹窗


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def _clear_pycache() -> None:
    """清理查分核心与插件的 __pycache__（防止源码更新后旧字节码被复用）"""
    import shutil as _shutil
    for d in (
        ROOT / "plugins" / "paradigm_score" / "__pycache__",
        Path(r"D:\Developed\ParadigmReboot-Tracker\core\__pycache__"),
    ):
        try:
            _shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def main() -> None:
    log("daemon start")
    _clear_pycache()
    while True:
        log("launching bot.py")
        try:
            BOT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with BOT_LOG.open("ab") as f:
                proc = subprocess.run(
                    [str(PY), str(ROOT / "bot.py")],
                    cwd=str(ROOT),
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
            log(f"bot.py exited with code {proc.returncode}")
        except Exception as e:
            log(f"launch error: {e!r}")
        # 退出码 0 也重启（意外退出）；连续崩溃时 10s 间隔防止刷爆 CPU
        time.sleep(10)


if __name__ == "__main__":
    main()
