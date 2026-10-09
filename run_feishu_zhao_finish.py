"""Finish 小赵 for source already claimed to 小韩; then fix Feishu row."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient
from services.full_pipeline import qps_retry, wait_shop_detail
from services.publish_prep import apply_publish_prep
from services.publisher import publish_details
from services.transformer import transform_product

SOURCE = "1736822893662603157"
COMMON_ID = 3985726758
SHOP_ZHAO = 18545044
SHOP_HAN = 18545217
RECORD_ID = "recvuTO0k5Ttk0"  # second feishu row = Viva / 小赵 assignment


def feishu_update(settings, record_id, fields):
    f = settings["feishu"]
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": f["app_id"], "app_secret": f["app_secret"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    tok = json.loads(urllib.request.urlopen(req).read().decode())["tenant_access_token"]
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{f['bitable_app_token']}"
        f"/tables/{f['bitable_table_id']}/records/{record_id}"
    )
    req2 = urllib.request.Request(
        url,
        data=json.dumps({"fields": fields}).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="PUT",
    )
    return json.loads(urllib.request.urlopen(req2).read().decode())


def main():
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    m = settings["miaoshou"]
    client = MiaoshouClient(m["app_key"], m["app_secret"], timeout=120, min_interval=1.5)
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    pub_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    pub_cfg["shop_id"] = SHOP_ZHAO
    pub_cfg["shop_name"] = "小赵1店"
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")

    print("1) 按 sourceItemId 查 TikTok 采集箱…", flush=True)
    body = client.search_tiktok_box(page_no=1, page_size=50, status=None, source_item_id=SOURCE)
    lst = (body.get("data") or {}).get("detailList") or []
    print(f"  found {len(lst)}", flush=True)
    detail_id = None
    for it in lst:
        print(
            json.dumps(
                {
                    "collectBoxDetailId": it.get("collectBoxDetailId"),
                    "itemNum": it.get("itemNum"),
                    "title": (it.get("title") or "")[:40],
                    "shops": [
                        s.get("shopId")
                        for s in (it.get("collectBoxDetailShopList") or [])
                        if isinstance(s, dict)
                    ],
                    "status": it.get("status"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        did = it.get("collectBoxDetailId")
        if did:
            detail_id = int(did)

    if not detail_id:
        # try from known first-run mapping
        detail_id = 3371025071
        print(f"  fallback detail_id={detail_id}", flush=True)

    print(f"\n2) claim_to_shops detail={detail_id} → 小赵1店", flush=True)
    try:
        resp = qps_retry(lambda: client.claim_to_shops([detail_id], [SHOP_ZHAO]), "claim_zhao")
        print("claim", json.dumps(resp, ensure_ascii=False)[:800], flush=True)
    except Exception as e:
        print("claim warn", e, flush=True)

    time.sleep(3)
    print("\n3) 改品…", flush=True)
    qps_retry(lambda: client.claim_to_shops([detail_id], [SHOP_ZHAO]), "reclaim")
    time.sleep(1)
    oss, product = wait_shop_detail(client, detail_id, SHOP_ZHAO, log=print)
    report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
    report["product"].pop("_pending_local_images", None)
    prep = apply_publish_prep(report["product"], pub_cfg)
    save = qps_retry(
        lambda: client.save_shop_detail(oss, report["product"], detail_id, SHOP_ZHAO),
        "save",
    )
    print("save", save.get("code") if isinstance(save, dict) else save, "prep", prep, flush=True)
    time.sleep(1.2)
    _, saved = wait_shop_detail(client, detail_id, SHOP_ZHAO, log=print)
    print(
        f"title={(saved.get('title') or '')[:50]} imgs={len(saved.get('imgUrls') or [])}",
        flush=True,
    )

    print("\n4) 上架…", flush=True)
    pub = publish_details(
        client,
        [detail_id],
        pub_cfg,
        dry_run=False,
        confirm=True,
        prep_before_publish=False,
    )
    print("publish", json.dumps(pub, ensure_ascii=False)[:1000], flush=True)
    calls = pub.get("publish_calls") or []
    ok = bool(calls) and not pub.get("error")

    now_ms = int(time.time() * 1000)
    upd = feishu_update(
        settings,
        RECORD_ID,
        {"入库状态": "是", "入库时间": now_ms, "上架状态": "是" if ok else "否", "上架时间": now_ms},
    )
    print("feishu", upd.get("code"), upd.get("msg"), flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"feishu_e2e_zhao_fix_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "source": SOURCE,
                "common_id": COMMON_ID,
                "detail_id": detail_id,
                "publish": pub,
                "ok": ok,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print("done", path, "ok=", ok, flush=True)


if __name__ == "__main__":
    main()
