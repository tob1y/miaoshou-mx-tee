"""Re-edit the two known 小赵1店 items: transform rules + fixed pre-tax price 369.99.

NOTE: OpenAPI get_price_template_list returns empty; apply-template route not found.
When user pastes 引用模板 API, wire it before transform_product.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.transformer import transform_product

SHOP_ID = 18545044
TARGETS = [
    {"detail_id": 3363991520, "item_num": "1734861195775280263"},
    {"detail_id": 3363991508, "item_num": "1737236462415480315"},
]


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

    # Try locate price template (often empty on open API)
    tpl_note = {"found": False, "list_total": None, "error": None}
    try:
        path = "/open/v1/product/collect_box/tiktok/price_template/get_price_template_list"
        body = client.assert_success(
            client.post(path, {"pageNo": 1, "pageSize": 20, "name": "墨西哥t恤", "site": "MX"}),
            "定价模板",
        )
        lst = ((body.get("data") or {}).get("priceTemplateList")) or []
        tpl_note["list_total"] = (body.get("data") or {}).get("total")
        tpl_note["names"] = [t.get("name") for t in lst]
        tpl_note["found"] = any("墨西哥" in str(t.get("name") or "") for t in lst)
    except Exception as e:
        tpl_note["error"] = str(e)

    results = []
    for t in TARGETS:
        detail_id = int(t["detail_id"])
        print(f"\n==== re-edit detailId={detail_id} itemNum={t['item_num']} ====")
        try:
            # Ensure claimed to shop
            client.claim_to_shops([detail_id], [SHOP_ID])
            time.sleep(1.5)
            oss, product = client.get_shop_detail(detail_id, SHOP_ID)
            before_prices = []
            for sku in (product.get("skuMap") or {}).values():
                if isinstance(sku, dict) and str(sku.get("isDelete") or "0") not in ("1", "true"):
                    before_prices.append(sku.get("price"))
            report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
            # no title mark / no remark mark
            report["product"].pop("_pending_local_images", None)
            save_body = client.save_shop_detail(oss, report["product"], detail_id, SHOP_ID)
            time.sleep(2)
            _, saved = client.get_shop_detail(detail_id, SHOP_ID)
            after_prices = []
            for sku in (saved.get("skuMap") or {}).values():
                if isinstance(sku, dict) and str(sku.get("isDelete") or "0") not in ("1", "true"):
                    after_prices.append(sku.get("price"))
            entry = {
                "ok": True,
                "detail_id": detail_id,
                "item_num": t["item_num"],
                "title": saved.get("title"),
                "sizes": report.get("sizes"),
                "colors": report.get("colors"),
                "price_report": report.get("price"),
                "prices_before": sorted(set(before_prices), key=lambda x: (x is None, x)),
                "prices_after": sorted(set(after_prices), key=lambda x: (x is None, x)),
                "save_code": save_body.get("code") if isinstance(save_body, dict) else None,
                "template_step": "skipped_openapi_empty",
            }
            results.append(entry)
            print("title:", (saved.get("title") or "")[:90])
            print("prices before:", entry["prices_before"])
            print("prices after:", entry["prices_after"])
            print("sizes:", report.get("sizes"))
        except Exception as e:
            results.append({"ok": False, "detail_id": detail_id, "item_num": t["item_num"], "error": str(e)})
            print("FAIL:", e)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"xiaozhao_reedit2_{stamp}.json"
    payload = {
        "shop_id": SHOP_ID,
        "scheme_price": (scheme.get("rules") or {}).get("price"),
        "price_template_name": scheme.get("price_template_name"),
        "template_lookup": tpl_note,
        "edited": results,
        "note": "引用模板 API 未接通：开放平台定价模板列表为空。已直接套用改品规则+税前价369.99。",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n报告:", path)
    print("模板查找:", tpl_note)
    ok = sum(1 for r in results if r.get("ok"))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
