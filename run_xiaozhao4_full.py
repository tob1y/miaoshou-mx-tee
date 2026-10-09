"""Full pipeline for 4 newest 小赵 public-box items → 小赵1店: claim → template → publish."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.publisher import publish_details
from services.publish_prep import apply_publish_prep
from services.transformer import transform_product

SHOP_ID = 18545044
SHOP_NAME = "小赵1店"
GROUP_NAME = "小赵"
WANT = 4


def qps_retry(fn, label: str, sleeps=(2, 4, 8, 12, 18)):
    last = None
    for i, wait in enumerate(sleeps, 1):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e)
            if any(x in msg for x in ("Qps", "Qpm", "频率", "rate", "未选择预发布")):
                print(f"  {label} retry {i}: {msg[:100]} -> sleep {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
    raise last  # type: ignore[misc]


def list_public_page1(client: MiaoshouClient) -> list[dict]:
    body = qps_retry(
        lambda: client.assert_success(
            client.post(ep.PUBLIC_LIST, {"pageNo": 1, "pageSize": 50}),
            "公共采集箱",
        ),
        "public_list",
    )
    return (body.get("data") or {}).get("detailList") or []


def claim_public(client: MiaoshouClient, items: list[dict]) -> dict[int, int]:
    detail_list = [
        {
            "detailId": int(it["commonCollectBoxDetailId"]),
            "platform": "tiktok",
            "serialNumber": i,
        }
        for i, it in enumerate(items, start=1)
    ]
    body = {
        "detailSerialNumberPlatformList": detail_list,
        "shopIds": [SHOP_ID],
    }
    resp = qps_retry(
        lambda: client.assert_success(client.post(ep.CLAIM_PUBLIC, body), "公共箱认领"),
        "claim_public",
    )
    id_map = ((resp.get("data") or {}).get("platformCollectBoxDetailIdMap") or {}).get("tiktok") or {}
    return {int(k): int(v) for k, v in id_map.items()}


def wait_detail(client: MiaoshouClient, detail_id: int) -> tuple[str, dict]:
    last: Exception | None = None
    for attempt in range(1, 15):
        try:
            oss, product = client.get_shop_detail(detail_id, SHOP_ID)
            if product.get("title") or product.get("skuMap") or product.get("skuPropertyList"):
                return oss, product
        except Exception as e:
            last = e
            print(f"  wait detail {detail_id} #{attempt}: {e}", flush=True)
        time.sleep(2.5)
    raise MiaoshouError(f"详情未就绪 {detail_id}: {last}")


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=2.0,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    pub_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("1) 拉公共采集箱，取「小赵」最新成功 4 条…", flush=True)
    public = list_public_page1(client)
    zhao = [
        it
        for it in public
        if (it.get("commonCollectBoxGroupName") or "").strip() == GROUP_NAME
        and str(it.get("status") or "").lower() == "success"
    ]
    zhao = sorted(zhao, key=lambda x: str(x.get("gmtCreate") or ""), reverse=True)[:WANT]
    print(f"   选中 {len(zhao)} 条:", flush=True)
    for it in zhao:
        print(
            f"   - common={it.get('commonCollectBoxDetailId')} "
            f"item={it.get('itemNum')} {(it.get('title') or '')[:50]}",
            flush=True,
        )
    if len(zhao) < WANT:
        print(f"WARNING: 只找到 {len(zhao)}/{WANT}")
    if not zhao:
        return 1

    print("\n2) 公共箱认领 → TikTok 采集箱…", flush=True)
    mapping = claim_public(client, zhao)
    print(f"   映射 {len(mapping)}: {mapping}", flush=True)
    detail_ids = list(mapping.values())
    time.sleep(2)

    print("\n3) claim_to_shop → 小赵1店…", flush=True)
    qps_retry(
        lambda: client.claim_to_shops(detail_ids, [SHOP_ID]),
        "claim_to_shop",
    )
    time.sleep(5)

    print("\n4) 套模板改品并保存…", flush=True)
    edited = []
    for it in zhao:
        common_id = int(it["commonCollectBoxDetailId"])
        detail_id = mapping.get(common_id)
        if not detail_id:
            edited.append({"ok": False, "common_id": common_id, "error": "no mapping"})
            continue
        print(f"\n[{detail_id}] 改品…", flush=True)
        try:
            qps_retry(lambda d=detail_id: client.claim_to_shops([d], [SHOP_ID]), "reclaim")
            time.sleep(1)
            oss, product = wait_detail(client, detail_id)
            before_title = product.get("title")
            report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
            report["product"].pop("_pending_local_images", None)
            # publish prep: package truncations
            prep = apply_publish_prep(report["product"], pub_cfg)
            save = qps_retry(
                lambda: client.save_shop_detail(oss, report["product"], detail_id, SHOP_ID),
                "save",
            )
            time.sleep(1.5)
            _, saved = wait_detail(client, detail_id)
            prices = sorted(
                {
                    s.get("price")
                    for s in (saved.get("skuMap") or {}).values()
                    if isinstance(s, dict) and str(s.get("isDelete") or "0") not in ("1", "true")
                }
            )
            entry = {
                "ok": True,
                "common_id": common_id,
                "detail_id": detail_id,
                "item_num": it.get("itemNum"),
                "before_title": before_title,
                "after_title": saved.get("title"),
                "prices": prices,
                "imgs": len(saved.get("imgUrls") or []),
                "attrs": len(saved.get("productAttributes") or []),
                "notes_has_detail": "picui.ogmua.cn" in str(saved.get("notes") or ""),
                "weight": saved.get("weight"),
                "pack": [
                    saved.get("packageLength"),
                    saved.get("packageWidth"),
                    saved.get("packageHeight"),
                ],
                "prep": prep,
                "save_code": save.get("code") if isinstance(save, dict) else None,
            }
            edited.append(entry)
            print(
                f"  OK price={prices} imgs={entry['imgs']} attrs={entry['attrs']} "
                f"detail_imgs={entry['notes_has_detail']}",
                flush=True,
            )
        except Exception as e:
            edited.append(
                {"ok": False, "common_id": common_id, "detail_id": detail_id, "error": str(e)}
            )
            print(f"  FAIL: {e}", flush=True)
            time.sleep(2)

    ok_ids = [int(r["detail_id"]) for r in edited if r.get("ok") and r.get("detail_id")]
    print(f"\n5) 上架发布 {len(ok_ids)} 条 → {SHOP_NAME}…", flush=True)
    publish_result = None
    if ok_ids:
        try:
            publish_result = publish_details(
                client,
                ok_ids,
                pub_cfg,
                dry_run=False,
                confirm=True,
                prep_before_publish=False,  # already prepped+saved above
            )
            print("  publish_calls:", json.dumps(publish_result.get("publish_calls"), ensure_ascii=False)[:800], flush=True)
        except Exception as e:
            publish_result = {"ok": False, "error": str(e)}
            print(f"  PUBLISH FAIL: {e}", flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"xiaozhao4_full_{stamp}.json"
    payload = {
        "shop_id": SHOP_ID,
        "shop_name": SHOP_NAME,
        "group": GROUP_NAME,
        "mapping": {str(k): v for k, v in mapping.items()},
        "edited": edited,
        "publish": publish_result,
        "ok_edit": sum(1 for r in edited if r.get("ok")),
        "fail_edit": sum(1 for r in edited if not r.get("ok")),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成 改品 {payload['ok_edit']}/{len(edited)} 报告: {path}", flush=True)
    return 0 if payload["ok_edit"] == len(edited) and edited else 1


if __name__ == "__main__":
    raise SystemExit(main())
