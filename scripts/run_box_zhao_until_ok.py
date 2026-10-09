# -*- coding: utf-8 -*-
"""采集箱未发布 → 用新规则改品上架到小赵，凑满 N 条成功。"""
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
from services.auto_batch import edit_and_publish_one, qps_retry
from services.daily_quota import DailyQuota

SHOP_ID = 18545044
SHOP_NAME = "小赵1店"
HAN_ID = 18545217
TARGET_OK = 20
MAX_TRY = 120


def list_not_published(client: MiaoshouClient) -> list[dict]:
    zhao: list[dict] = []
    han: list[dict] = []
    for page in range(1, 30):
        body = client.search_tiktok_box(page_no=page, page_size=100, status="notPublished")
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            shops = it.get("collectBoxDetailShopList") or []
            sids = [
                int(x["shopId"])
                for x in shops
                if isinstance(x, dict) and x.get("shopId") is not None
            ]
            row = {
                "detail_id": int(it["collectBoxDetailId"]),
                "title": str(it.get("title") or ""),
                "item_num": it.get("itemNum"),
                "from_shop": sids[0] if sids else None,
            }
            if SHOP_ID in sids:
                zhao.append(row)
            elif HAN_ID in sids:
                han.append(row)
        if len(batch) < 100:
            break
        time.sleep(0.2)
    # 小赵优先，再从小韩未发布箱挪到小赵
    return zhao + han


def main() -> int:
    target = TARGET_OK
    if len(sys.argv) > 1:
        target = int(sys.argv[1])

    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"box_zhao_until_ok_{stamp}.log"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
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

    rows = list_not_published(client)
    n_zhao = sum(1 for r in rows if r.get("from_shop") == SHOP_ID)
    n_han = sum(1 for r in rows if r.get("from_shop") == HAN_ID)
    log(
        f"目标成功 {target} → {SHOP_NAME}；未发布可取 {len(rows)} "
        f"(已在小赵 {n_zhao} / 从小韩挪 {n_han})；新规则含女装短袖T"
    )

    results: list[dict] = []
    ok = held = fail = 0
    tried = 0
    for row in rows:
        if ok >= target:
            break
        if tried >= MAX_TRY:
            log(f"已试 {MAX_TRY} 仍未凑满 {target}")
            break
        if quota.remaining(SHOP_ID) <= 0:
            log("配额用尽")
            break

        tried += 1
        did = int(row["detail_id"])
        log(f"[try {tried} ok={ok}/{target}] detail={did} {(row.get('title') or '')[:60]}")
        t0 = time.time()
        one: dict = {"detail_id": did, "item_num": row.get("item_num"), "from_shop": row.get("from_shop")}
        try:
            # 确保认领到小赵
            qps_retry(lambda: client.claim_to_shops([did], [SHOP_ID]), "claim_zhao", log=log)
            time.sleep(0.5)
            ep = edit_and_publish_one(
                client,
                detail_id=did,
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
                held += 1
                why = ((ep.get("hold_without_publish") or {}).get("reason") or "")[:80]
                log(f"  留库 {time.time()-t0:.1f}s {why}")
            elif ep.get("ok"):
                ok += 1
                quota.add(SHOP_ID, 1)
                log(f"  OK 已上架 {time.time()-t0:.1f}s ({ok}/{target})")
            else:
                fail += 1
                log(f"  FAIL {time.time()-t0:.1f}s")
        except Exception as e:
            fail += 1
            one["error"] = str(e)
            log(f"  FAIL {time.time()-t0:.1f}s {e}")
        results.append(one)
        (out_dir / f"box_zhao_until_ok_{stamp}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(0.6)

    summary = {
        "target": target,
        "tried": tried,
        "ok": ok,
        "held": held,
        "fail": fail,
        "shop": SHOP_NAME,
        "ok_details": [r.get("detail_id") for r in results if r.get("ok")],
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"box_zhao_until_ok_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok >= target else 1


if __name__ == "__main__":
    raise SystemExit(main())
