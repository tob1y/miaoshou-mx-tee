# -*- coding: utf-8 -*-
"""Deep-scan published listings for non-1-pack (pack qty attr + multipack title)."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient  # noqa: E402
from rules.titles import is_multipack_text  # noqa: E402

SHOPS = {18545217: "小韩1店", 18545044: "小赵1店"}
# 默认先扫小赵
PRIORITY = [18545044, 18545217]


def pack_qty(product: dict) -> str:
    for a in product.get("productAttributes") or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("attributeId") or "") != "100347":
            continue
        vals = a.get("attributeValues") or []
        names = [
            str(v.get("valueName") or v.get("name") or "").strip()
            for v in vals
            if isinstance(v, dict)
        ]
        names = [n for n in names if n]
        return ",".join(names) if names else ""
    return ""


def main() -> None:
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--only="):
            only = a.split("=", 1)[1].strip()

    priority = list(PRIORITY)
    if only == "zhao":
        priority = [18545044]
    elif only == "han":
        priority = [18545217]

    s = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = s.get("miaoshou") or {}
    c = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        timeout=90,
        min_interval=0.95,
    )
    out_dir = ROOT / "data/previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"multipack_deep_scan_{stamp}.log"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    rows = []
    for page in range(1, 50):
        body = c.search_tiktok_box(page_no=page, page_size=100, status="published")
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
            our = [sid for sid in sids if sid in SHOPS]
            if not our:
                continue
            sid = our[0]
            if sid not in priority:
                continue
            rows.append(
                {
                    "detail_id": int(it["collectBoxDetailId"]),
                    "shop_id": sid,
                    "shop": SHOPS[sid],
                    "list_title": str(it.get("title") or ""),
                    "item_num": it.get("itemNum"),
                }
            )
        if len(batch) < 100:
            break
        time.sleep(0.2)

    # 按店铺优先级排序（小赵优先）
    order = {sid: i for i, sid in enumerate(priority)}
    rows.sort(key=lambda r: (order.get(r["shop_id"], 99), r["detail_id"]))

    by_shop = {}
    for r in rows:
        by_shop[r["shop"]] = by_shop.get(r["shop"], 0) + 1
    log(f"published target: {len(rows)} {by_shop} priority={[SHOPS[s] for s in priority]}")

    bad = []
    ok_one = 0
    missing_attr = 0
    errors = 0
    for i, row in enumerate(rows, 1):
        if i % 20 == 0 or i == 1:
            log(
                f"progress {i}/{len(rows)} shop={row['shop']} "
                f"bad={len(bad)} ok1={ok_one} miss_attr={missing_attr} err={errors}"
            )
        try:
            try:
                c.claim_to_shops([row["detail_id"]], [row["shop_id"]])
            except Exception:
                pass
            time.sleep(0.2)
            _oss, p = c.get_shop_detail(row["detail_id"], row["shop_id"])
            title = str(p.get("title") or row["list_title"] or "")
            qty = pack_qty(p)
            title_mp = is_multipack_text(title) or is_multipack_text(row["list_title"])
            qty_bad = bool(qty) and qty.strip() not in ("1",)
            qty_missing = qty == ""
            if qty_missing:
                missing_attr += 1
            if qty_bad or title_mp:
                bad.append(
                    {
                        **row,
                        "detail_title": title[:160],
                        "pack_qty": qty or "(无)",
                        "title_multipack": title_mp,
                        "qty_bad": qty_bad,
                    }
                )
                log(
                    f"BAD {row['shop']} {row['detail_id']} qty={qty or '(无)'} "
                    f"title_mp={title_mp} | {title[:80]}"
                )
            elif not qty_missing:
                ok_one += 1
        except Exception as e:
            errors += 1
            if "已被删除" in str(e):
                continue
            log(f"ERR {row['detail_id']}: {e}")

    summary = {
        "scanned": len(rows),
        "priority": [SHOPS[s] for s in priority],
        "confirmed_non_one_or_title_mp": len(bad),
        "pack_qty_is_1": ok_one,
        "missing_pack_attr": missing_attr,
        "errors": errors,
        "bad": bad,
    }
    path = out_dir / f"multipack_deep_scan_{stamp}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(
        f"=== DONE scanned={len(rows)} bad={len(bad)} qty1={ok_one} "
        f"miss_attr={missing_attr} err={errors} -> {path.name}"
    )


if __name__ == "__main__":
    main()
