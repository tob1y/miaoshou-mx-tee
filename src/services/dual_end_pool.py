# -*- coding: utf-8 -*-
"""两店倒着啃今日公共箱：共享已处理 sid，避免撞货。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def shared_path(root: Path) -> Path:
    return root / "data/previews/dual_end_today.json"


def load_shared(root: Path, today: str) -> set[str]:
    p = shared_path(root)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if str(data.get("date") or "") != today:
        return set()
    sids = data.get("sids") or {}
    if isinstance(sids, dict):
        return {str(k) for k in sids.keys() if k}
    if isinstance(sids, list):
        return {str(x) for x in sids if x}
    return set()


def mark_shared(root: Path, today: str, sid: str, who: str) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    p = shared_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        data: dict = {"date": today, "sids": {}}
        if p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
                if str(old.get("date") or "") == today and isinstance(old.get("sids"), dict):
                    data["sids"] = dict(old["sids"])
            except Exception:
                pass
        data["sids"][sid] = who
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
