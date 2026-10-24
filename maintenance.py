# -*- coding: utf-8 -*-
"""维护脚本：日志轮转 + 运势归档

用法：
    python maintenance.py            # 执行全部维护
    python maintenance.py --logs     # 只轮转日志
    python maintenance.py --fortune  # 只归档运势
    python maintenance.py --dry-run  # 只报告将要做什么

设计要点：
- 日志轮转必须【先停 bot 再操作】：bot 运行时 bot.log 被 daemon 的追加句柄占用，
  无法重命名；原地截断会让 daemon 的写入偏移错位（产生 NUL 空洞）。
  流程：停计划任务 → 归档 gzip → 清空重建 → 启计划任务（约 20 秒停机）
- 运势归档：把早于 N 个月（默认 3）的签文从主文件移出到归档 gzip。
  保留近期记录，保证「脱凶检测」（需昨日数据）正常工作。
"""
import argparse
import gzip
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(r"D:\Developed\Nymphea-bot")
LOGS = ROOT / "logs"
ARCHIVE = ROOT / "data" / "archive"
FORTUNE = ROOT / "data" / "fortune_data.json"
TASK_NAME = "NympheaBot"

LOG_FILES = ("bot.log", "daemon.log", "debug8000.log")
LOG_KEEP = 8          # 日志归档保留份数
FORTUNE_RETENTION_MONTHS = 3


def step(msg: str):
    print(f"[maint] {msg}", flush=True)


def _month_key(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}"


def _months_ago(d: date, months: int) -> date:
    """返回 d 往前 months 个月的日期（月末自动收敛）"""
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


# ---------- 任务控制 ----------

def _run_ps(cmd: str) -> tuple[bool, str]:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def stop_bot() -> bool:
    ok, out = _run_ps(
        "$ErrorActionPreference='SilentlyContinue';"
        f"Stop-ScheduledTask -TaskName '{TASK_NAME}';"
        "Start-Sleep -Seconds 2;"
        "Get-Process pythonw | Where-Object { $_.Path -like '*Nymphea-bot*' } | Stop-Process -Force;"
        "Start-Sleep -Seconds 2;"
        "'stopped'"
    )
    return "stopped" in out


def start_bot() -> bool:
    ok, out = _run_ps(
        "$ErrorActionPreference='SilentlyContinue';"
        f"Start-ScheduledTask -TaskName '{TASK_NAME}';"
        "'started'"
    )
    return "started" in out


# ---------- 日志轮转 ----------

def rotate_logs(dry: bool = False) -> dict:
    """把 logs/*.log 压缩归档到 logs/archive/，保留最近 LOG_KEEP 份"""
    LOGS.mkdir(parents=True, exist_ok=True)
    log_archive = LOGS / "archive"

    targets = [(LOGS / n) for n in LOG_FILES if (LOGS / n).exists() and (LOGS / n).stat().st_size > 0]
    if not targets:
        return {"rotated": 0, "note": "no non-empty logs"}

    total = sum(p.stat().st_size for p in targets)
    if dry:
        return {"rotated": len(targets), "bytes": total, "note": "dry-run"}

    log_archive.mkdir(parents=True, exist_ok=True)
    iso = date.today().isocalendar()
    tag = f"{iso[0]}W{iso[1]:02d}"
    rotated = 0
    freed = 0
    for p in targets:
        size = p.stat().st_size
        dst = log_archive / f"{p.stem}_{tag}.log.gz"
        try:
            # 追加模式：gzip 支持多 member 串接，同名归档已有内容时追加而非覆盖
            mode = "ab" if dst.exists() else "wb"
            with p.open("rb") as src, gzip.open(dst, mode, compresslevel=9) as out:
                out.write(f"\n===== rotated at {datetime.now():%Y-%m-%d %H:%M:%S} =====\n".encode())
                shutil.copyfileobj(src, out)
            p.unlink()
            rotated += 1
            freed += size
        except Exception as e:
            step(f"  rotate {p.name} FAILED: {e!r}")

    # 清理超量归档
    archives = sorted(log_archive.glob("*.log.gz"), key=lambda x: x.name)
    for old in archives[:-LOG_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return {"rotated": rotated, "bytes": freed, "archives": len(archives)}


# ---------- 运势归档 ----------

def archive_fortune(dry: bool = False) -> dict:
    """把早于 FORTUNE_RETENTION_MONTHS 个月的签文移到 data/archive/"""
    if not FORTUNE.exists():
        return {"moved": 0, "note": "no fortune file"}
    data = json.loads(FORTUNE.read_text(encoding="utf-8-sig"))
    users = data.get("users") or {}
    if not users:
        return {"moved": 0, "note": "empty"}

    cutoff = _months_ago(date.today(), FORTUNE_RETENTION_MONTHS)
    cutoff_key = cutoff.isoformat()

    moved_users: dict[str, dict] = {}
    total_moved = 0
    for uid, records in users.items():
        if not isinstance(records, dict):
            continue
        keep, old = {}, {}
        for dkey, rec in records.items():
            # 日期键形如 YYYY-MM-DD；解析失败的一律保留
            if isinstance(dkey, str) and len(dkey) == 10 and dkey < cutoff_key:
                old[dkey] = rec
            else:
                keep[dkey] = rec
        if old:
            moved_users[uid] = old
            total_moved += len(old)
            users[uid] = keep

    if not total_moved:
        return {"moved": 0, "cutoff": cutoff_key, "note": "nothing to archive"}

    result = {"moved": total_moved, "cutoff": cutoff_key, "users": len(moved_users)}
    if dry:
        result["note"] = "dry-run"
        return result

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    dst = ARCHIVE / f"fortune_before_{cutoff_key}.json.gz"
    # 追加式归档：若同名归档已存在则合并
    existing = {}
    if dst.exists():
        try:
            with gzip.open(dst, "rt", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    merged = existing.get("users") or {}
    for uid, recs in moved_users.items():
        merged.setdefault(uid, {}).update(recs)
    payload = {
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "cutoff": cutoff_key,
        "retention_months": FORTUNE_RETENTION_MONTHS,
        "users": merged,
    }
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # 清理已无记录的用户键
    data["users"] = {u: r for u, r in users.items() if r}
    FORTUNE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    result["archive_file"] = dst.name
    return result


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", action="store_true")
    ap.add_argument("--fortune", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    do_logs = args.logs or not (args.logs or args.fortune)
    do_fortune = args.fortune or not (args.logs or args.fortune)

    start_ts = time.time()
    step(f"start (logs={do_logs} fortune={do_fortune} dry={args.dry_run})")

    # 日志轮转需要独占访问日志文件 → 先停 bot
    bot_stopped = False
    if do_logs and not args.dry_run:
        bot_stopped = stop_bot()
        step(f"1) bot stopped: {bot_stopped}")

    if do_logs:
        r = rotate_logs(args.dry_run)
        step(f"2) logs rotated: {r}")
    if do_fortune:
        r = archive_fortune(args.dry_run)
        step(f"3) fortune archived: {r}")

    if bot_stopped:
        time.sleep(1)
        started = start_bot()
        step(f"4) bot started: {started}")

    step(f"done in {time.time()-start_ts:.1f}s")


if __name__ == "__main__":
    main()
