# -*- coding: utf-8 -*-
"""小赵今日配额到 TRIGGER 后自动开小韩并发上架（两店并行）。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUOTA = ROOT / "data/daily_quota.json"
ZHAO = "18545044"
TODAY = datetime.now().strftime("%Y-%m-%d")
# 小赵到此数就开小韩；小赵自己继续跑到 300
TRIGGER = 230


def zhao_count() -> int:
    try:
        data = json.loads(QUOTA.read_text(encoding="utf-8"))
        return int((data.get(TODAY) or {}).get(ZHAO) or 0)
    except Exception:
        return 0


def main() -> int:
    print(
        f"{datetime.now():%H:%M:%S} watch Zhao→Han parallel; "
        f"today={TODAY} trigger={TRIGGER}",
        flush=True,
    )
    while True:
        n = zhao_count()
        print(f"{datetime.now():%H:%M:%S} Zhao quota={n}/300 (Han at {TRIGGER})", flush=True)
        if n >= TRIGGER:
            break
        time.sleep(30)
    print(f"{datetime.now():%H:%M:%S} Zhao≥{TRIGGER} → start Han (Zhao keeps running)", flush=True)
    log = ROOT / "data/previews" / f"han_autostart_{datetime.now():%Y%m%d_%H%M%S}.log"
    with log.open("w", encoding="utf-8") as f:
        p = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "scripts/process_han_concurrent.py")],
            cwd=str(ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONIOENCODING": "utf-8"},
        )
    print(f"Han pid={p.pid} log={log.name}", flush=True)
    return p.wait()


if __name__ == "__main__":
    raise SystemExit(main())
