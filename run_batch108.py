"""Batch-edit the newly claimed 108 unpublished items for 小赵1店."""
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

SHOP_ID = 18545044
EXPECTED = 108


def list_targets(client: MiaoshouClient) -> list[dict]:
    items: list[dict] = []
    for page in range(1, 8):
        body = client.search_tiktok_box(page_no=page, page_size=100, status="notPublished")
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        time.sleep(0.8)
    # 本批 108 条当前可能挂在别的店；流水线统一认领到小赵1店后改品
    return items


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=1.5,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = list_targets(client)
    print(f"targets (unpublished): {len(targets)} -> claim+edit shop {SHOP_ID}")
    if len(targets) != EXPECTED:
        print(f"WARNING: expected ~{EXPECTED}, got {len(targets)}")

    results = []
    t0 = time.time()
    for i, it in enumerate(targets, 1):
        detail_id = int(it["collectBoxDetailId"])
        item_num = str(it.get("itemNum") or "")
        group = (it.get("collectBoxGroupName") or "").strip() or "(empty)"
        print(f"\n[{i}/{len(targets)}] detailId={detail_id} itemNum={item_num} group={group}", flush=True)
        try:
            client.claim_to_shops([detail_id], [SHOP_ID])
            time.sleep(0.8)
            oss, product = client.get_shop_detail(detail_id, SHOP_ID)
            before_title = product.get("title")
            report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
            report["product"].pop("_pending_local_images", None)
            save = client.save_shop_detail(oss, report["product"], detail_id, SHOP_ID)
            time.sleep(1.2)
            _, saved = client.get_shop_detail(detail_id, SHOP_ID)
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
                "item_num": item_num,
                "group": group,
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
                "colors_mode": (report.get("colors") or {}).get("mode"),
                "sku_cap": report.get("sku_cap"),
                "save_code": save.get("code") if isinstance(save, dict) else None,
            }
            results.append(entry)
            print(
                f"  OK imgs={entry['imgs']} price={prices} pack={entry['pack']} "
                f"title={(saved.get('title') or '')[:60]}",
                flush=True,
            )
        except Exception as e:
            results.append(
                {
                    "ok": False,
                    "detail_id": detail_id,
                    "item_num": item_num,
                    "group": group,
                    "error": str(e),
                }
            )
            print(f"  FAIL: {e}", flush=True)
            time.sleep(2)

        if i % 10 == 0:
            ck = out_dir / f"batch108_checkpoint_{i}.json"
            ck.write_text(
                json.dumps(
                    {
                        "done": i,
                        "ok": sum(1 for r in results if r.get("ok")),
                        "fail": sum(1 for r in results if not r.get("ok")),
                        "elapsed_sec": round(time.time() - t0, 1),
                        "edited": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"  checkpoint -> {ck}", flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"batch108_{stamp}.json"
    ok_n = sum(1 for r in results if r.get("ok"))
    payload = {
        "shop_id": SHOP_ID,
        "total": len(results),
        "ok": ok_n,
        "fail": len(results) - ok_n,
        "elapsed_sec": round(time.time() - t0, 1),
        "edited": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成 {ok_n}/{len(results)} 用时 {payload['elapsed_sec']}s 报告: {path}")
    return 0 if ok_n == len(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
