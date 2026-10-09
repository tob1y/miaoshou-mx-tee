"""Publish orchestration for TikTok collect-box → shop listing."""
from __future__ import annotations

import time
from typing import Any

from api.miaoshou_client import MiaoshouClient
from services.publish_prep import apply_publish_prep, build_publish_api_body


def chunked(items: list[int], size: int) -> list[list[int]]:
    size = max(1, int(size))
    return [items[i : i + size] for i in range(0, len(items), size)]


def filter_unpublished(
    client: MiaoshouClient,
    detail_ids: list[int],
    *,
    enabled: bool,
    shop_id: int | None = None,
) -> tuple[list[int], list[int]]:
    """If anti_duplicate.filter_published: drop ids already published.

    Bugfix: when the same collectBoxDetailId is shared by two shops, only skip
    if *this* shop_id already appears on a published row (or shop_id is None).
    """
    if not enabled or not detail_ids:
        return list(detail_ids), []

    want = {int(x) for x in detail_ids}
    published: set[int] = set()
    for page in range(1, 30):
        body = client.search_tiktok_box(page_no=page, page_size=100, status="published")
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            try:
                did = int(it.get("collectBoxDetailId"))
            except Exception:
                continue
            if did not in want:
                continue
            if shop_id is None:
                published.add(did)
                continue
            shops = it.get("collectBoxDetailShopList") or []
            shop_ids = {
                int(s.get("shopId"))
                for s in shops
                if isinstance(s, dict) and s.get("shopId") is not None
            }
            # if shop list empty, be conservative and skip
            if not shop_ids or int(shop_id) in shop_ids:
                published.add(did)
        if len(batch) < 100:
            break
        time.sleep(0.6)

    keep = [d for d in detail_ids if d not in published]
    skipped = [d for d in detail_ids if d in published]
    return keep, skipped


def prep_and_save_one(
    client: MiaoshouClient,
    detail_id: int,
    shop_id: int,
    cfg: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    oss, product = client.get_shop_detail(detail_id, shop_id)
    prep = apply_publish_prep(product, cfg)
    entry: dict[str, Any] = {
        "detail_id": detail_id,
        "prep": prep,
        "dry_run": dry_run,
    }
    if dry_run:
        entry["saved"] = False
        entry["title"] = product.get("title")
        entry["weight"] = product.get("weight")
        entry["pack"] = [
            product.get("packageLength"),
            product.get("packageWidth"),
            product.get("packageHeight"),
        ]
        return entry
    save = client.save_shop_detail(oss, product, detail_id, shop_id)
    entry["saved"] = True
    entry["save_code"] = save.get("code") if isinstance(save, dict) else None
    return entry


def publish_details(
    client: MiaoshouClient,
    detail_ids: list[int],
    cfg: dict[str, Any],
    *,
    dry_run: bool = True,
    confirm: bool = False,
    prep_before_publish: bool = True,
) -> dict[str, Any]:
    """Publish flow. Safe by default: dry_run=True does not call publish API."""
    shop_id = int(cfg.get("shop_id") or 0)
    if not shop_id:
        raise ValueError("publish config missing shop_id")

    require_confirm = bool(cfg.get("require_confirm", True))
    if not dry_run and require_confirm and not confirm:
        raise PermissionError("真实发布需要 confirm=True（或 CLI --confirm）")

    anti = cfg.get("anti_duplicate") or {}
    keep, skipped = filter_unpublished(
        client,
        [int(x) for x in detail_ids],
        enabled=bool(anti.get("filter_published")),
        shop_id=shop_id,
    )

    prep_results: list[dict[str, Any]] = []
    if prep_before_publish:
        for did in keep:
            # claim ensure shop selected
            if not dry_run:
                try:
                    client.claim_to_shops([did], [shop_id])
                    time.sleep(0.8)
                except Exception as e:
                    prep_results.append(
                        {"detail_id": did, "ok": False, "stage": "claim", "error": str(e)}
                    )
                    continue
            try:
                import json as _json

                if dry_run:
                    _, product = client.get_shop_detail(did, shop_id)
                    p2 = _json.loads(_json.dumps(product))
                    prep = apply_publish_prep(p2, cfg)
                    prep_results.append(
                        {
                            "ok": True,
                            "detail_id": did,
                            "prep": prep,
                            "dry_run": True,
                            "title": p2.get("title"),
                            "weight": p2.get("weight"),
                            "pack": [
                                p2.get("packageLength"),
                                p2.get("packageWidth"),
                                p2.get("packageHeight"),
                            ],
                        }
                    )
                else:
                    row = prep_and_save_one(client, did, shop_id, cfg, dry_run=False)
                    row["ok"] = True
                    prep_results.append(row)
                    time.sleep(float(cfg.get("min_interval_seconds") or 2.0))
            except Exception as e:
                prep_results.append(
                    {"detail_id": did, "ok": False, "stage": "prep", "error": str(e)}
                )

    ready_ids = [r["detail_id"] for r in prep_results if r.get("ok")]
    # if prep skipped entirely, publish keep list
    if not prep_before_publish:
        ready_ids = list(keep)

    batches = chunked(ready_ids, int(cfg.get("batch_size") or 20))
    publish_calls: list[dict[str, Any]] = []
    for batch in batches:
        api_body = build_publish_api_body(batch, [shop_id], cfg)
        if dry_run:
            publish_calls.append({"dry_run": True, "would_post": api_body, "ok": True})
            continue
        try:
            resp = client.publish_collect_items(batch, [shop_id], extra=api_body.get("publishOptions"))
            publish_calls.append({"dry_run": False, "ok": True, "batch": batch, "response": resp})
            time.sleep(float(cfg.get("min_interval_seconds") or 2.0))
        except Exception as e:
            publish_calls.append({"dry_run": False, "ok": False, "batch": batch, "error": str(e)})

    return {
        "shop_id": shop_id,
        "shop_name": cfg.get("shop_name"),
        "dry_run": dry_run,
        "requested": len(detail_ids),
        "skipped_published": skipped,
        "prep": prep_results,
        "publish_calls": publish_calls,
        "ready": ready_ids,
        "config_snapshot": {
            "publish_mode": cfg.get("publish_mode"),
            "package": cfg.get("package"),
            "auto_translate": cfg.get("auto_translate"),
            "anti_duplicate": cfg.get("anti_duplicate"),
            "product_title": cfg.get("product_title"),
            "sku_spec": {
                k: (cfg.get("sku_spec") or {}).get(k)
                for k in (
                    "attr_name_max_chars",
                    "attr_value_max_chars",
                    "auto_delete_when_over_250_skus",
                )
            },
            "prohibited_words": cfg.get("prohibited_words"),
        },
    }
