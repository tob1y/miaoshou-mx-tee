# -*- coding: utf-8 -*-
"""公共箱 success（按手动采集开始时间过滤）→ 认领改品上架小韩；记录总耗时。"""
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
# 用户手动采集开始时间
MANUAL_START = datetime(2026, 9, 17, 19, 12, 0, tzinfo=TZ)
TARGET = 100


def list_public_success_since(client: MiaoshouClient, since: datetime, limit: int) -> list[dict]:
    """拉公共箱，筛 status=success 且 gmtCreate >= since，新的优先，最多 limit 条。"""
    since_naive = since.replace(tzinfo=None)  # API 时间多为本地无 tz 字符串
    since_ts = since.timestamp()
    rows: list[dict] = []
    for page in range(1, 40):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "public",
            ),
            "public_list",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if page == 1:
            print(f"public_total={(body.get('data') or {}).get('total')}", flush=True)
        if not batch:
            break
        for it in batch:
            if str(it.get("status") or "").lower() != "success":
                continue
            gmt = str(it.get("gmtCreate") or "").strip()
            # 常见 "2026-09-17 19:15:01"
            ok_time = False
            if gmt:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                    try:
                        dt = datetime.strptime(gmt[:26], fmt).replace(tzinfo=TZ)
                        if dt >= since:
                            ok_time = True
                        break
                    except ValueError:
                        continue
            if not ok_time:
                continue
            cid = it.get("commonCollectBoxDetailId")
            sid = str(it.get("itemNum") or it.get("sourceItemId") or "")
            if cid is None:
                continue
            rows.append(
                {
                    "common_id": int(cid),
                    "sid": sid,
                    "title": str(it.get("title") or "")[:80],
                    "gmt": gmt,
                }
            )
        if len(batch) < 100:
            break
        time.sleep(0.25)

    # 去重 common_id，按时间升序（先采的在前）或降序——用升序贴近「那一批」
    uniq: dict[int, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("gmt") or ""):
        uniq[r["common_id"]] = r
    out = list(uniq.values())
    return out[:limit]


def main() -> int:
    target = TARGET
    if len(sys.argv) > 1:
        target = int(sys.argv[1])

    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"public_success_han_timing_{stamp}.log"
    result_path = out_dir / f"public_success_han_timing_{stamp}.json"

    t_wall0 = time.perf_counter()
    wall_start = datetime.now(TZ)

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(
        f"TIMER_START wall={wall_start.isoformat()} "
        f"manual_collect_since={MANUAL_START.isoformat()} → {SHOP_NAME} target={target}"
    )

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

    t_find0 = time.perf_counter()
    log("开始寻找公共箱 success（>=19:12）…")
    pool = list_public_success_since(client, MANUAL_START, limit=target)
    find_sec = time.perf_counter() - t_find0
    log(f"找到 success {len(pool)} 条（寻找耗时 {find_sec:.1f}s）")
    if pool:
        log(f"  最早 {pool[0].get('gmt')} 最晚 {pool[-1].get('gmt')}")

    if not pool:
        log("TIMER_END 无可用 success，退出")
        return 1

    take = pool[: min(target, len(pool), quota.remaining(SHOP_ID))]
    log(f"本批处理 {len(take)} 条 → {SHOP_NAME}；配额剩 {quota.remaining(SHOP_ID)}")

    results = []
    ok = held = fail = 0
    t_pub0 = time.perf_counter()
    for i, row in enumerate(take, 1):
        if quota.remaining(SHOP_ID) <= 0:
            log("配额用尽")
            break
        sid = str(row.get("sid") or "")
        common_id = int(row["common_id"])
        log(f"[{i}/{len(take)}] common={common_id} sid={sid} {(row.get('title') or '')[:50]}")
        t0 = time.time()
        one: dict = {
            "common_id": common_id,
            "source_id": sid,
            "gmt": row.get("gmt"),
            "shop_id": SHOP_ID,
            "ok": False,
        }
        try:
            if not sid:
                raise RuntimeError("缺少 itemNum/source_id")
            public_item = {
                "commonCollectBoxDetailId": common_id,
                "itemNum": sid,
                "status": "success",
            }
            detail_id = claim_to_detail(client, public_item, SHOP_ID, sid, log)
            one["detail_id"] = detail_id
            ep = edit_and_publish_one(
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
            one.update(ep)
            if ep.get("held_in_box"):
                held += 1
                log(f"  留库 {time.time()-t0:.1f}s")
            elif ep.get("ok"):
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
        result_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(0.5)

    pub_sec = time.perf_counter() - t_pub0
    total_sec = time.perf_counter() - t_wall0
    wall_end = datetime.now(TZ)
    summary = {
        "timer_start": wall_start.isoformat(),
        "timer_end": wall_end.isoformat(),
        "manual_collect_since": MANUAL_START.isoformat(),
        "shop": SHOP_NAME,
        "found": len(pool),
        "processed": len(results),
        "ok": ok,
        "held": held,
        "fail": fail,
        "find_seconds": round(find_sec, 1),
        "publish_seconds": round(pub_sec, 1),
        "total_seconds": round(total_sec, 1),
        "total_minutes": round(total_sec / 60, 2),
        "quota_han": quota.count(SHOP_ID),
    }
    log(f"=== TIMER_END {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"public_success_han_timing_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
