"""Retry batch108_han failures: force re-claim then edit on 小韩1店."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient
from services.transformer import transform_product

SHOP_ID = 18545217
REPORT = ROOT / "data" / "previews" / "batch108_han_20260910_152244.json"


def qps_retry(fn, label: str, sleeps=(3, 6, 10, 15, 20)):
    last = None
    for i, wait in enumerate(sleeps, 1):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e)
            if "Qps" in msg or "频率" in msg or "rate" in msg.lower() or "未选择预发布" in msg:
                print(f"  {label} retry {i}: {msg[:80]} -> sleep {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
    raise last  # type: ignore[misc]


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=2.5,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")

    prev = json.loads(REPORT.read_text(encoding="utf-8"))
    fail_ids = [int(x["detail_id"]) for x in prev["edited"] if not x.get("ok")]
    print(f"retry {len(fail_ids)} fails on 小韩1店", flush=True)

    results = []
    t0 = time.time()
    for i, detail_id in enumerate(fail_ids, 1):
        print(f"\n[{i}/{len(fail_ids)}] detailId={detail_id}", flush=True)
        try:
            # always force claim
            qps_retry(lambda: client.claim_to_shops([detail_id], [SHOP_ID]), "claim")
            time.sleep(1.5)
            oss, product = qps_retry(
                lambda: client.get_shop_detail(detail_id, SHOP_ID), "get"
            )
            before_title = product.get("title")
            report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
            report["product"].pop("_pending_local_images", None)
            save = qps_retry(
                lambda: client.save_shop_detail(oss, report["product"], detail_id, SHOP_ID),
                "save",
            )
            time.sleep(1.5)
            _, saved = qps_retry(lambda: client.get_shop_detail(detail_id, SHOP_ID), "verify")
            prices = sorted(
                {
                    s.get("price")
                    for s in (saved.get("skuMap") or {}).values()
                    if isinstance(s, dict) and str(s.get("isDelete") or "0") not in ("1", "true")
                }
            )
            entry = {
                "ok": True,
                "detail_id": detail_id,
                "before_title": before_title,
                "after_title": saved.get("title"),
                "prices": prices,
                "imgs": len(saved.get("imgUrls") or []),
                "pack": [
                    saved.get("packageLength"),
                    saved.get("packageWidth"),
                    saved.get("packageHeight"),
                ],
                "weight": saved.get("weight"),
                "save_code": save.get("code") if isinstance(save, dict) else None,
            }
            results.append(entry)
            print(f"  OK price={prices} imgs={entry['imgs']}", flush=True)
        except Exception as e:
            results.append({"ok": False, "detail_id": detail_id, "error": str(e)})
            print(f"  FAIL: {e}", flush=True)
            time.sleep(3)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"batch108_han_retry42_{stamp}.json"
    ok_n = sum(1 for r in results if r.get("ok"))
    payload = {
        "shop_id": SHOP_ID,
        "shop_name": "小韩1店",
        "total": len(results),
        "ok": ok_n,
        "fail": len(results) - ok_n,
        "elapsed_sec": round(time.time() - t0, 1),
        "edited": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n重试完成 {ok_n}/{len(results)} 报告: {path}")
    return 0 if ok_n == len(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
