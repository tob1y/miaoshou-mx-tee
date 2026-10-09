# -*- coding: utf-8 -*-
"""公共箱：最近手动补采的 success → 认领改品上架小韩。

会先等 processing 消化，再处理 since 之后的 success。
用法：
  python scripts/run_public_success_han_recent.py
  python scripts/run_public_success_han_recent.py 21:10
  python scripts/run_public_success_han_recent.py 2026-09-17T21:10
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one, qps_retry
from services.daily_quota import DailyQuota

SHOP_ID = 18545217
SHOP_NAME = "小韩1店"
TZ = timezone(timedelta(hours=8))


def parse_since(arg: str | None) -> datetime:
    now = datetime.now(TZ)
    if not arg:
        return now - timedelta(minutes=25)
    arg = arg.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%H:%M"):
        try:
            dt = datetime.strptime(arg, fmt)
            if fmt == "%H:%M":
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    raise SystemExit(f"无法解析时间: {arg}")


def parse_gmt(gmt: str) -> datetime | None:
    gmt = str(gmt or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(gmt[:26], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def scan_public(client: MiaoshouClient, since: datetime, max_pages: int = 30) -> dict:
    by_status: dict[str, int] = {}
    success: list[dict] = []
    processing: list[dict] = []
    for page in range(1, max_pages + 1):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "public",
            ),
            "public_list",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        page_has_recent = False
        for it in batch:
            dt = parse_gmt(str(it.get("gmtCreate") or ""))
            if not dt or dt < since:
                continue
            page_has_recent = True
            st = str(it.get("status") or "").lower() or "(empty)"
            by_status[st] = by_status.get(st, 0) + 1
            cid = it.get("commonCollectBoxDetailId")
            sid = str(it.get("itemNum") or it.get("sourceItemId") or "")
            row = {
                "common_id": int(cid) if cid is not None else None,
                "sid": sid,
                "title": str(it.get("title") or "")[:80],
                "gmt": str(it.get("gmtCreate") or ""),
                "status": st,
                "public_item": it,
            }
            if st == "success" and cid is not None and sid:
                success.append(row)
            elif st == "processing":
                processing.append(row)
        # newest-first: if whole page older than since, stop
        oldest = parse_gmt(str(batch[-1].get("gmtCreate") or ""))
        if oldest and oldest < since and not page_has_recent:
            break
        if len(batch) < 100:
            break
        time.sleep(0.35)
    # dedupe success by common_id
    uniq: dict[int, dict] = {}
    for r in sorted(success, key=lambda x: x.get("gmt") or ""):
        uniq[int(r["common_id"])] = r
    return {
        "by_status": by_status,
        "success": list(uniq.values()),
        "processing_n": len(processing),
    }


def main() -> int:
    since_arg = sys.argv[1] if len(sys.argv) > 1 else None
    since = parse_since(since_arg)
    wait_rounds = 12
    wait_sleep = 20

    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"public_success_han_recent_{stamp}.log"
    result_path = out_dir / f"public_success_han_recent_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
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

    log(f"公共箱 recent success → {SHOP_NAME}；since={since.isoformat()}；配额剩 {quota.remaining(SHOP_ID)}")

    pool: list[dict] = []
    for round_i in range(1, wait_rounds + 1):
        snap = scan_public(client, since)
        log(
            f"轮询 #{round_i}: status={snap['by_status']} "
            f"success={len(snap['success'])} processing≈{snap['processing_n']}"
        )
        pool = snap["success"]
        if snap["processing_n"] == 0:
            break
        log(f"  仍有 processing，{wait_sleep}s 后再看…")
        time.sleep(wait_sleep)

    if not pool:
        log("无 success 可上架，结束")
        return 1

    log(f"准备上架 {len(pool)} 条（最早 {pool[0]['gmt']} 最晚 {pool[-1]['gmt']}）")
    take = pool[: quota.remaining(SHOP_ID)]
    results = []
    ok = held = fail = 0
    for i, row in enumerate(take, 1):
        if quota.remaining(SHOP_ID) <= 0:
            log("配额用尽")
            break
        sid = row["sid"]
        common_id = int(row["common_id"])
        log(f"[{i}/{len(take)}] common={common_id} sid={sid} {row.get('title') or ''}")
        t0 = time.time()
        one: dict = {"common_id": common_id, "source_id": sid, "gmt": row.get("gmt"), "ok": False}
        try:
            public_item = {
                "commonCollectBoxDetailId": common_id,
                "itemNum": sid,
                "status": "success",
            }
            detail_id = claim_to_detail(client, public_item, SHOP_ID, sid, log)
            one["detail_id"] = detail_id
            ep_res = edit_and_publish_one(
                client,
                detail_id=detail_id,
                shop_id=SHOP_ID,
                shop_name=SHOP_NAME,
                scheme=scheme,
                publish_cfg=publish_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                log=log,
            )
            one.update(ep_res)
            if ep_res.get("held_in_box"):
                held += 1
                log(f"  留库 {time.time()-t0:.1f}s")
            elif ep_res.get("ok"):
                ok += 1
                quota.add(SHOP_ID, 1)
                log(f"  OK 已上架 {time.time()-t0:.1f}s detail={detail_id} ({ok})")
            else:
                fail += 1
                log(f"  FAIL publish {time.time()-t0:.1f}s")
        except Exception as e:
            fail += 1
            one["error"] = str(e)
            log(f"  FAIL {time.time()-t0:.1f}s {e}")
        results.append(one)
        result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        time.sleep(0.5)

    summary = {
        "since": since.isoformat(),
        "shop": SHOP_NAME,
        "found": len(pool),
        "processed": len(results),
        "ok": ok,
        "held": held,
        "fail": fail,
        "quota_han": quota.count(SHOP_ID),
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"public_success_han_recent_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
