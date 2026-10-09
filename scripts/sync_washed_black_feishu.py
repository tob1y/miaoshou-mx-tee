# -*- coding: utf-8 -*-
"""把本地水洗黑命中同步到飞书表（默认最新 N 条）。

用法:
  py -3.12 scripts/sync_washed_black_feishu.py          # 最新 3 条
  py -3.12 scripts/sync_washed_black_feishu.py --n=10
  py -3.12 scripts/sync_washed_black_feishu.py --all
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from services.washed_black_feishu import WashedBlackFeishuSink


def main() -> int:
    n = 3
    push_all = False
    for a in sys.argv[1:]:
        if a == "--all":
            push_all = True
        elif a.startswith("--n="):
            n = int(a.split("=", 1)[1])

    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    f = settings.get("feishu") or {}
    token = str(f.get("washed_black_sheet_token") or "").strip()
    if not token:
        print("missing washed_black_sheet_token")
        return 2

    js = sorted(
        (ROOT / "data/previews").glob("washed_black_cleanup_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not js:
        print("no washed_black_cleanup json")
        return 1
    data = json.loads(js[-1].read_text(encoding="utf-8"))
    hits = data.get("hits") or []
    batch = hits if push_all else hits[-n:]
    print("source", js[-1].name, "total_hits", len(hits), "push", len(batch))

    sink = WashedBlackFeishuSink(
        str(f.get("app_id") or ""),
        str(f.get("app_secret") or ""),
        token,
        log=print,
    )
    if not sink.ensure():
        print("FAIL: 飞书表不可用。请给应用开通 sheets:spreadsheet 权限，并把该表格添加为应用可用文档。")
        print(
            "开通入口: https://open.feishu.cn/app/cli_aa29098bc9b9dbd5/auth"
            "?q=sheets:spreadsheet&op_from=openapi&token_type=tenant"
        )
        print("表格: https://acnekf8nnaii.feishu.cn/sheets/XaHIsjo3ZhUntwtXEPGcHypqnMD")
        return 3

    ok = sink.append_hits(batch)
    print("appended", ok)
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
