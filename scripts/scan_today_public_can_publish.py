# -*- coding: utf-8 -*-
"""今天公共箱 success（排除已处理赵批次）做门禁预检。不认领、不保存、不上架。"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.auto_batch import qps_retry
from services.transformer import transform_product

TZ = timezone(timedelta(hours=8))
SHOP_NOTE = "预检不上架"


def parse_gmt(s: str) -> datetime | None:
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def adapt_public_product(prod: dict) -> dict:
    """公共箱详情 → 门禁用的 skuPropertyList（仅内存，不写回）。"""
    out = dict(prod)
    color_name = str(out.get("colorPropName") or "Color")
    size_name = str(out.get("sizePropName") or "Tamaño")
    color_vals = []
    for key, val in (out.get("colorMap") or {}).items():
        name = val.get("name") if isinstance(val, dict) else key
        color_vals.append({"attrValue": str(name or key), "attrValueId": str(key)})
    size_vals = []
    for key, val in (out.get("sizeMap") or {}).items():
        name = val.get("name") if isinstance(val, dict) else key
        size_vals.append({"attrValue": str(name or key), "attrValueId": str(key)})
    props = []
    if color_vals:
        props.append({"name": color_name, "attrValueList": color_vals})
    if size_vals:
        props.append({"name": size_name, "attrValueList": size_vals})
    out["skuPropertyList"] = props
    return out


def load_excluded() -> set[str]:
    log = ROOT / "data/previews/feishu_zhao_batch_20260918_144959.log"
    if not log.exists():
        return set()
    text = log.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"解析 \[\d+/140\] source=(\d+)", text))


def list_today_success(client: MiaoshouClient, excluded: set[str], today0: datetime) -> list[dict]:
    rows: dict[int, dict] = {}
    for page in range(1, 40):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "public",
            ),
            f"list_{page}",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        page_today = 0
        for it in batch:
            dt = parse_gmt(str(it.get("gmtCreate") or ""))
            if not dt or dt < today0:
                continue
            page_today += 1
            if str(it.get("status") or "").lower() != "success":
                continue
            sid = str(it.get("itemNum") or it.get("sourceItemId") or "")
            if sid in excluded:
                continue
            cid = it.get("commonCollectBoxDetailId")
            if cid is None:
                continue
            rows[int(cid)] = {
                "common_id": int(cid),
                "sid": sid,
                "title": str(it.get("title") or "")[:120],
                "gmt": str(it.get("gmtCreate") or ""),
            }
        oldest = parse_gmt(str(batch[-1].get("gmtCreate") or ""))
        print(f"  list page {page}: today={page_today} keep={len(rows)}", flush=True)
        if oldest and oldest < today0:
            break
        if len(batch) < 100:
            break
        time.sleep(0.3)
    return list(rows.values())


def main() -> int:
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    excluded = load_excluded()
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    scheme = json.loads((ROOT / "config/schemes/default.json").read_text(encoding="utf-8"))
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets/blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data/previews")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = out_dir / f"public_can_publish_{stamp}.json"
    summary_path = out_dir / f"public_can_publish_summary_{stamp}.json"

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        timeout=90,
        min_interval=0.7,
        max_retries=4,
    )

    print(f"排除已处理货源 {len(excluded)}；扫描今天 success…", flush=True)
    pool = list_today_success(client, excluded, today0)
    print(f"待预检 {len(pool)}（不上架）", flush=True)

    results: list[dict] = []
    reasons = Counter()
    can = hold = err = 0
    for i, row in enumerate(pool, 1):
        one = dict(row)
        try:
            body = qps_retry(
                lambda cid=row["common_id"]: client.post(
                    ep.PUBLIC_DETAIL, {"commonCollectBoxDetailId": int(cid)}
                ),
                "detail",
            )
            inner = (body.get("body") or body).get("data") or {}
            if "result" in (body.get("body") or {}) and (body.get("body") or {}).get("result") != "success":
                raise RuntimeError(str((body.get("body") or {}).get("message") or body)[:180])
            prod = inner.get("editCommonCollectBoxDetail") or {}
            if not prod:
                raise RuntimeError("无 editCommonCollectBoxDetail")
            adapted = adapt_public_product(prod)
            report = transform_product(adapted, scheme, blank, fill_image_urls=fill_urls)
            hold_r = report.get("hold_without_publish") or {}
            one["hold"] = bool(hold_r.get("hold"))
            one["reason"] = str(hold_r.get("reason") or "ok")
            one["can_publish"] = not one["hold"]
            if one["can_publish"]:
                can += 1
            else:
                hold += 1
                reasons[one["reason"]] += 1
        except Exception as e:
            err += 1
            one["error"] = str(e)[:200]
            one["can_publish"] = False
            reasons["preview_error"] += 1
        results.append(one)
        if i % 25 == 0 or i == len(pool):
            print(f"  [{i}/{len(pool)}] 可上架={can} 留库={hold} 出错={err}", flush=True)
            result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.15)

    summary = {
        "today": today0.date().isoformat(),
        "excluded_processed_sids": len(excluded),
        "scanned": len(pool),
        "can_publish": can,
        "hold": hold,
        "error": err,
        "can_publish_rate": round(can / len(pool), 3) if pool else 0,
        "hold_reasons": dict(reasons),
        "note": "仅公共箱详情门禁预检，未认领、未保存、未上架",
        "result_file": str(result_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== 预检结束 ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
