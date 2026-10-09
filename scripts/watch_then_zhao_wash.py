# -*- coding: utf-8 -*-
"""等今日小韩上架进程结束（小赵已满则不等）→ 只扫小赵水洗黑 → 出今日报表。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUOTA = ROOT / "data/daily_quota.json"
PREV = ROOT / "data/previews"
TODAY = datetime.now().strftime("%Y-%m-%d")
HAN = "18545217"
ZHAO = "18545044"
LIMIT = 300


def quota_counts() -> tuple[int, int]:
    try:
        data = json.loads(QUOTA.read_text(encoding="utf-8"))
        d = data.get(TODAY) or {}
        return int(d.get(ZHAO) or 0), int(d.get(HAN) or 0)
    except Exception:
        return 0, 0


def han_python_alive() -> bool:
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
            except Exception:
                continue
            if "process_han_concurrent" in cmd:
                return True
    except Exception:
        pass
    # fallback: wmic/tasklist via powershell-less check
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*process_han_concurrent*' } | "
                "Measure-Object | Select-Object -ExpandProperty Count",
            ],
            text=True,
            timeout=30,
        )
        return int((out or "0").strip() or "0") > 0
    except Exception:
        return False


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def main() -> int:
    log(f"wait shops finish then Zhao wash-black; today={TODAY}")
    while True:
        z, h = quota_counts()
        alive = han_python_alive()
        log(f"quota 赵={z}/{LIMIT} 韩={h}/{LIMIT} han_proc={'yes' if alive else 'no'}")
        # 两店都满，或韩进程已结束且赵已满（韩可能提前撞池结束）
        if z >= LIMIT and (h >= LIMIT or not alive):
            break
        if not alive and z >= LIMIT:
            # 韩进程没了但未满：再等一轮确认不是短暂空窗
            time.sleep(45)
            if not han_python_alive():
                z2, h2 = quota_counts()
                log(f"韩进程结束 最终配额 赵={z2} 韩={h2} → 开始后续")
                break
            continue
        time.sleep(60)

    z, h = quota_counts()
    log(f"上架阶段结束 赵={z} 韩={h} → 启动小赵水洗黑扫描")

    scan_log = PREV / f"washed_zhao_after_publish_{datetime.now():%Y%m%d_%H%M%S}.log"
    with scan_log.open("w", encoding="utf-8") as f:
        p = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "scripts/scan_washed_black_cleanup.py"), "--only=zhao"],
            cwd=str(ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONIOENCODING": "utf-8"},
        )
    log(f"wash scan pid={p.pid} log={scan_log.name}")
    rc = p.wait()
    log(f"wash scan exit={rc}")

    report = ROOT / "scripts" / "export_today_publish_and_wash_report.py"
    if report.exists():
        log("生成今日上架+水洗黑报表…")
        r2 = subprocess.call(
            [sys.executable, "-u", str(report)],
            cwd=str(ROOT),
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONIOENCODING": "utf-8"},
        )
        log(f"report exit={r2}")
    else:
        log(f"报表脚本尚未就绪: {report.name}（上架后会补跑）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
