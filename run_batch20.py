"""Batch-edit all unpublished TikTok collect-box items for 小赵1店."""
from __future__ import annotations

import json
import sys
time_mod = __import__("time")
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.transformer import transform_product

SHOP_ID = 18545044


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=1.6,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)

    body = client.search_tiktok_box(page_no=1, page_size=100, status="notPublished")
    items = (body.get("data") or {}).get("detailList") or []
    # only 小赵1店
    targets = []
    for it in items:
        shops = it.get("collectBoxDetailShopList") or []
        sids = {str(s.get("shopId")) for s in shops if isinstance(s, dict)}
        if str(SHOP_ID) in sids or not shops:
            targets.append(it)
    print(f"unpublished for shop {SHOP_ID}: {len(targets)}")

    results = []
    for i, it in enumerate(targets, 1):
        detail_id = int(it["collectBoxDetailId"])
        item_num = str(it.get("itemNum") or "")
        print(f"\n[{i}/{len(targets)}] detailId={detail_id} itemNum={item_num}")
        try:
            client.claim_to_shops([detail_id], [SHOP_ID])
            time_mod.sleep(1.2)
            oss, product = client.get_shop_detail(detail_id, SHOP_ID)
            before_title = product.get("title")
            before_imgs = len(product.get("imgUrls") or [])
            report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
            report["product"].pop("_pending_local_images", None)
            save = client.save_shop_detail(oss, report["product"], detail_id, SHOP_ID)
            time_mod.sleep(1.8)
            _, saved = client.get_shop_detail(detail_id, SHOP_ID)
            prices = sorted(
                {
                    s.get("price")
                    for s in (saved.get("skuMap") or {}).values()
                    if isinstance(s, dict) and str(s.get("isDelete") or "0") not in ("1", "true")
                }
            )
            sizes = []
            for prop in saved.get("skuPropertyList") or []:
                name = str(prop.get("attrName") or prop.get("name") or "").lower()
                if "size" in name or "talla" in name:
                    sizes = [
                        v.get("attrValue")
                        for v in (prop.get("attrValueList") or [])
                        if isinstance(v, dict)
                    ]
            entry = {
                "ok": True,
                "detail_id": detail_id,
                "item_num": item_num,
                "before_title": before_title,
                "after_title": saved.get("title"),
                "before_imgs": before_imgs,
                "after_imgs": len(saved.get("imgUrls") or []),
                "prices": prices,
                "pack": [
                    saved.get("packageLength"),
                    saved.get("packageWidth"),
                    saved.get("packageHeight"),
                ],
                "weight": saved.get("weight"),
                "sizes": sizes,
                "colors": report.get("colors"),
                "images": report.get("images"),
                "save_code": save.get("code") if isinstance(save, dict) else None,
            }
            results.append(entry)
            print("  OK title:", (saved.get("title") or "")[:70])
            print("  price", prices, "imgs", entry["after_imgs"], "pack", entry["pack"])
        except Exception as e:
            results.append(
                {
                    "ok": False,
                    "detail_id": detail_id,
                    "item_num": item_num,
                    "error": str(e),
                }
            )
            print("  FAIL:", e)
            time_mod.sleep(2)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"batch20_{stamp}.json"
    ok_n = sum(1 for r in results if r.get("ok"))
    payload = {
        "shop_id": SHOP_ID,
        "total": len(results),
        "ok": ok_n,
        "fail": len(results) - ok_n,
        "scheme_price": (scheme.get("rules") or {}).get("price"),
        "dimensions": scheme.get("dimensions"),
        "fill_image_urls_n": len(fill_urls),
        "edited": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成 {ok_n}/{len(results)} 报告: {path}")
    return 0 if ok_n == len(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
