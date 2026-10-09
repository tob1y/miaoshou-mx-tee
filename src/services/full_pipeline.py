"""Reusable claim → template-edit → publish pipeline for GUI/CLI."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.publish_prep import apply_publish_prep
from services.publisher import publish_details
from services.transformer import transform_product

LogFn = Callable[[str], None]


@dataclass
class PipelineResult:
    shop_id: int
    shop_name: str
    mapping: dict[int, int] = field(default_factory=dict)
    edited: list[dict[str, Any]] = field(default_factory=list)
    publish: dict[str, Any] | None = None
    report_path: str | None = None


def qps_retry(fn, label: str, log: LogFn | None = None, sleeps=(3, 5, 8, 12, 18, 25)):
    last = None
    _log = log or (lambda _m: None)
    for i, wait in enumerate(sleeps, 1):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e)
            if any(
                x in msg
                for x in (
                    "Qps",
                    "Qpm",
                    "QPS",
                    "QPM",
                    "accountApiQpsRateLimit",
                    "频率",
                    "rate",
                    "RateLimit",
                    "未选择预发布",
                )
            ):
                _log(f"{label} 限流重试 {i}: {msg[:100]}，等待 {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last  # type: ignore[misc]


def list_public_items(client: MiaoshouClient, page_size: int = 50, log: LogFn | None = None) -> list[dict]:
    body = qps_retry(
        lambda: client.assert_success(
            client.post(ep.PUBLIC_LIST, {"pageNo": 1, "pageSize": min(100, page_size)}),
            "公共采集箱",
        ),
        "public_list",
        log=log,
    )
    return (body.get("data") or {}).get("detailList") or []


def claim_public_to_shop(
    client: MiaoshouClient,
    items: list[dict],
    shop_id: int,
    log: LogFn | None = None,
) -> dict[int, int]:
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
        "shopIds": [int(shop_id)],
    }
    resp = qps_retry(
        lambda: client.assert_success(client.post(ep.CLAIM_PUBLIC, body), "公共箱认领"),
        "claim_public",
        log=log,
    )
    id_map = ((resp.get("data") or {}).get("platformCollectBoxDetailIdMap") or {}).get("tiktok") or {}
    return {int(k): int(v) for k, v in id_map.items()}


def wait_shop_detail(
    client: MiaoshouClient,
    detail_id: int,
    shop_id: int,
    log: LogFn | None = None,
) -> tuple[str, dict]:
    last: Exception | None = None
    _log = log or (lambda _m: None)
    for attempt in range(1, 15):
        try:
            oss, product = client.get_shop_detail(detail_id, shop_id)
            if product.get("title") or product.get("skuMap") or product.get("skuPropertyList"):
                return oss, product
        except Exception as e:
            last = e
            _log(f"等待详情 {detail_id} #{attempt}: {e}")
        time.sleep(2.2)
    raise MiaoshouError(f"详情未就绪 {detail_id}: {last}")


def run_full_pipeline(
    client: MiaoshouClient,
    *,
    items: list[dict],
    shop_id: int,
    shop_name: str,
    scheme: dict[str, Any],
    publish_cfg: dict[str, Any],
    blank_dir: Path,
    fill_urls: list[str],
    out_dir: Path,
    do_publish: bool = True,
    log: LogFn | None = None,
) -> PipelineResult:
    _log = log or print
    result = PipelineResult(shop_id=shop_id, shop_name=shop_name)
    if not items:
        raise ValueError("没有选中商品")

    publish_cfg = dict(publish_cfg)
    publish_cfg["shop_id"] = int(shop_id)
    publish_cfg["shop_name"] = shop_name

    _log(f"认领 {len(items)} 条 → {shop_name} ({shop_id})")
    mapping = claim_public_to_shop(client, items, shop_id, log=_log)
    result.mapping = mapping
    _log(f"认领映射 {len(mapping)}: {mapping}")
    detail_ids = list(mapping.values())
    time.sleep(1.5)

    _log("claim_to_shop…")
    qps_retry(lambda: client.claim_to_shops(detail_ids, [shop_id]), "claim_to_shop", log=_log)
    time.sleep(4)

    _log("套模板改品…")
    for it in items:
        common_id = int(it["commonCollectBoxDetailId"])
        detail_id = mapping.get(common_id)
        if not detail_id:
            result.edited.append({"ok": False, "common_id": common_id, "error": "no mapping"})
            continue
        _log(f"改品 detailId={detail_id}")
        try:
            qps_retry(lambda d=detail_id: client.claim_to_shops([d], [shop_id]), "reclaim", log=_log)
            time.sleep(0.8)
            oss, product = wait_shop_detail(client, detail_id, shop_id, log=_log)
            before_title = product.get("title")
            report = transform_product(product, scheme, blank_dir, fill_image_urls=fill_urls)
            report["product"].pop("_pending_local_images", None)
            prep = apply_publish_prep(report["product"], publish_cfg)
            save = qps_retry(
                lambda: client.save_shop_detail(oss, report["product"], detail_id, shop_id),
                "save",
                log=_log,
            )
            time.sleep(1.2)
            _, saved = wait_shop_detail(client, detail_id, shop_id, log=_log)
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
                "hold_without_publish": report.get("hold_without_publish"),
            }
            hold = report.get("hold_without_publish") or {}
            if hold.get("hold"):
                entry["held_in_box"] = True
                entry["publish_eligible"] = False
                _log(
                    f"  留库不上架: 标题无具体颜色且选项无颜色 "
                    f"(options={hold.get('options', {}).get('reason')})"
                )
            else:
                entry["publish_eligible"] = True
                _log(
                    f"  OK price={prices} imgs={entry['imgs']} attrs={entry['attrs']} "
                    f"detail={entry['notes_has_detail']}"
                )
            result.edited.append(entry)
        except Exception as e:
            result.edited.append(
                {"ok": False, "common_id": common_id, "detail_id": detail_id, "error": str(e)}
            )
            _log(f"  FAIL: {e}")
            time.sleep(1.5)

    ok_ids = [
        int(r["detail_id"])
        for r in result.edited
        if r.get("ok") and r.get("detail_id") and r.get("publish_eligible", True) and not r.get("held_in_box")
    ]
    held_n = sum(1 for r in result.edited if r.get("held_in_box"))
    if held_n:
        _log(f"留库不上架 {held_n} 条（标题+选项均无具体颜色）")
    if do_publish and ok_ids:
        _log(f"上架发布 {len(ok_ids)} 条…")
        try:
            result.publish = publish_details(
                client,
                ok_ids,
                publish_cfg,
                dry_run=False,
                confirm=True,
                prep_before_publish=False,
            )
            calls = result.publish.get("publish_calls") or []
            _log(f"发布结果: {json.dumps(calls, ensure_ascii=False)[:500]}")
        except Exception as e:
            result.publish = {"ok": False, "error": str(e)}
            _log(f"发布失败: {e}")
    elif do_publish:
        _log("没有可发布的成功改品，跳过上架")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"pipeline_gui_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "shop_id": shop_id,
                "shop_name": shop_name,
                "mapping": {str(k): v for k, v in mapping.items()},
                "edited": result.edited,
                "publish": result.publish,
                "ok_edit": sum(1 for r in result.edited if r.get("ok")),
                "fail_edit": sum(1 for r in result.edited if not r.get("ok")),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    result.report_path = str(path)
    _log(f"报告: {path}")
    return result
