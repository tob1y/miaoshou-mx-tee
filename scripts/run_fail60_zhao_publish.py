# -*- coding: utf-8 -*-
"""把失败表里已手动采进公用箱(success)的链接，改品上架到小赵1店。"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one
from services.daily_quota import DailyQuota

SHOP_ID = 18545044
SHOP_NAME = "小赵1店"
PLAN = ROOT / "data/previews/fail60_public_ok.json"


def main() -> int:
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"fail60_zhao_publish_{stamp}.log"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    data = json.loads(PLAN.read_text(encoding="utf-8"))
    ok_rows = data.get("ok") or []
    # 按货源去重
    uniq: dict[str, dict] = {}
    for r in ok_rows:
        sid = str(r.get("sid") or "")
        if not sid:
            continue
        if sid not in uniq:
            uniq[sid] = r
    targets = list(uniq.values())
    log(f"公用箱 success {len(ok_rows)} 条记录，去重后 {len(targets)} 条 → {SHOP_NAME}")

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=1.2,
        max_retries=3,
    )
    scheme = json.loads((ROOT / "config/schemes/default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config/publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets/blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    quota = DailyQuota(
        ROOT / "data/daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )

    results = []
    n_ok = n_hold = n_fail = 0
    for i, row in enumerate(targets, 1):
        sid = str(row["sid"])
        common_id = int(row["common_id"])
        log(f"[{i}/{len(targets)}] source={sid} common={common_id} {(row.get('title') or '')[:50]}")
        t0 = time.time()
        one: dict = {
            "source_id": sid,
            "common_id": common_id,
            "link": row.get("link"),
            "shop_id": SHOP_ID,
            "shop_name": SHOP_NAME,
            "ok": False,
        }
        try:
            public_item = {
                "commonCollectBoxDetailId": common_id,
                "itemNum": sid,
                "status": "success",
            }
            detail_id = claim_to_detail(client, public_item, SHOP_ID, sid, log)
            one["detail_id"] = detail_id
            ep = edit_and_publish_one(
                client,
                detail_id=detail_id,
                shop_id=SHOP_ID,
                shop_name=SHOP_NAME,
                scheme=scheme,
                publish_cfg=publish_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                log=log,
            )
            one.update(ep)
            if ep.get("held_in_box"):
                n_hold += 1
                log(f"  留库 {time.time()-t0:.1f}s")
            elif ep.get("ok"):
                n_ok += 1
                quota.add(SHOP_ID, 1)
                log(f"  OK 已上架 {time.time()-t0:.1f}s detail={detail_id}")
            else:
                n_fail += 1
                log(f"  FAIL publish {time.time()-t0:.1f}s")
        except Exception as e:
            n_fail += 1
            one["error"] = str(e)
            log(f"  FAIL {time.time()-t0:.1f}s {e}")
        results.append(one)
        (out_dir / f"fail60_zhao_publish_{stamp}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(0.8)

    summary = {
        "targets": len(targets),
        "ok": n_ok,
        "held": n_hold,
        "fail": n_fail,
        "shop": SHOP_NAME,
        "quota_zhao": quota.count(SHOP_ID),
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"fail60_zhao_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
