# -*- coding: utf-8 -*-
"""试下架：指定货源ID，小韩店删采集箱明细。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import qps_retry

SID = "1737660880136799668"
HAN = 18545217
DELETE_PATH = "/open/v1/product/collect_box/tiktok/collect_box/delete_collect_box_detail"


def main() -> None:
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
    m = settings["miaoshou"]
    client = MiaoshouClient(
        m["app_key"],
        m["app_secret"],
        m.get("base_url"),
        timeout=90,
        min_interval=1.1,
    )

    found: list[dict] = []
    for status in ("published", "notPublished", "timingPublish"):
        body = qps_retry(
            lambda st=status: client.search_tiktok_box(
                page_no=1, page_size=50, status=st, source_item_id=SID
            ),
            f"search_{status}",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        print(f"status={status} n={len(batch)}")
        for it in batch:
            item_num = str(it.get("itemNum") or "")
            shops = it.get("collectBoxDetailShopList") or []
            shop_ids = [
                int(x["shopId"])
                for x in shops
                if isinstance(x, dict) and x.get("shopId") is not None
            ]
            print(
                f"  detail={it.get('collectBoxDetailId')} itemNum={item_num} "
                f"shops={shop_ids} title={(it.get('title') or '')[:60]}"
            )
            if item_num == SID or SID in item_num:
                found.append({"status": status, "item": it, "shop_ids": shop_ids})

    if not found:
        print("NOT_FOUND")
        return

    # prefer Han shop
    target = None
    for f in found:
        if HAN in f["shop_ids"]:
            target = f
            break
    if target is None:
        target = found[0]

    it = target["item"]
    detail_id = int(it["collectBoxDetailId"])
    shop_id = HAN if HAN in target["shop_ids"] else (target["shop_ids"][0] if target["shop_ids"] else HAN)
    print(f"DELETE detail={detail_id} shop={shop_id} status={target['status']}")

    resp = qps_retry(
        lambda: client.assert_success(
            client.post(DELETE_PATH, {"detailIds": [detail_id], "shopIds": [shop_id]}),
            "删除采集箱明细",
        ),
        "delete",
    )
    print("DELETE_RESP", json.dumps(resp, ensure_ascii=False)[:500])

    time.sleep(1.5)
    # verify
    body2 = qps_retry(
        lambda: client.search_tiktok_box(
            page_no=1, page_size=20, status="published", source_item_id=SID
        ),
        "verify",
    )
    left = (body2.get("data") or {}).get("detailList") or []
    still = [
        x
        for x in left
        if str(x.get("itemNum") or "") == SID
        and any(
            int(s.get("shopId") or 0) == shop_id
            for s in (x.get("collectBoxDetailShopList") or [])
            if isinstance(s, dict)
        )
    ]
    print(f"VERIFY published still_on_han={len(still)}")
    out = {
        "source_id": SID,
        "detail_id": detail_id,
        "shop_id": shop_id,
        "delete_resp": resp,
        "still_published_on_han": len(still),
    }
    path = ROOT / "data/previews" / f"takedown_one_{SID}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("WROTE", path)


if __name__ == "__main__":
    main()
