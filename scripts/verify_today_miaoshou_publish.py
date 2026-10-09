# -*- coding: utf-8 -*-
"""核实妙手今日两店实际上架量（发布记录 + published 采集箱）。"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.auto_batch import qps_retry

TZ = timezone(timedelta(hours=8))
SHOPS = {
    18545044: "小赵1店",
    18545217: "小韩1店",
}


def parse_gmt(s: str) -> datetime | None:
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    # ms epoch
    try:
        n = int(float(s))
        if n > 10_000_000_000:
            n //= 1000
        if n > 1_700_000_000:
            return datetime.fromtimestamp(n, tz=TZ)
    except Exception:
        pass
    return None


def list_key(data: dict) -> list:
    for k in ("moveCollectDetailList", "detailList", "list", "records"):
        if isinstance(data.get(k), list):
            return data[k]
    return []


def main() -> int:
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    cfg = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = cfg["miaoshou"]
    client = MiaoshouClient(
        m["app_key"],
        m["app_secret"],
        timeout=90,
        min_interval=1.8,
        max_retries=5,
    )

    print("=== 发布记录（今天）===")
    time.sleep(2)
    probe = qps_retry(lambda: client.search_publish_tasks(page_no=1, page_size=3), "probe")
    pdata = probe.get("data") or {}
    print("data keys", list(pdata.keys()), "total", pdata.get("total"))
    sample = list_key(pdata)
    if sample:
        print("sample keys", list(sample[0].keys()))
        print("sample compact", json.dumps({k: sample[0].get(k) for k in list(sample[0].keys())[:30]}, ensure_ascii=False)[:800])

    pub_today: dict[int, list] = {sid: [] for sid in SHOPS}
    status_by_shop: dict[int, dict] = {sid: {} for sid in SHOPS}

    for shop_id, name in SHOPS.items():
        time.sleep(2)
        rows_today = []
        for page in range(1, 50):
            body = qps_retry(
                lambda p=page, s=shop_id: client.search_publish_tasks(page_no=p, page_size=20, shop_id=s),
                f"pub_{shop_id}_{page}",
            )
            data = body.get("data") or {}
            lst = list_key(data)
            if page == 1:
                print(f"\n{name} total={data.get('total')} page1={len(lst)}")
            if not lst:
                break
            old_streak = 0
            for it in lst:
                # time fields
                dt = None
                for k in (
                    "gmtCreate",
                    "gmtModified",
                    "publishTime",
                    "createTime",
                    "taskCreateTime",
                    "moveTime",
                    "successTime",
                ):
                    if it.get(k) is not None and str(it.get(k)).strip():
                        dt = parse_gmt(str(it.get(k)))
                        if dt:
                            break
                # nested shop
                shop_ok = True
                st = str(it.get("status") or it.get("taskStatus") or it.get("moveStatus") or "")
                if dt and dt >= today0:
                    rows_today.append(it)
                    status_by_shop[shop_id][st or "(empty)"] = status_by_shop[shop_id].get(st or "(empty)", 0) + 1
                    old_streak = 0
                elif dt and dt < today0:
                    old_streak += 1
                else:
                    # no time — include first pages only
                    if page <= 2:
                        rows_today.append(it)
            print(f"  page {page}: n={len(lst)} today_acc={len(rows_today)} first_t={lst[0].get('gmtCreate') or lst[0].get('publishTime')}")
            if old_streak >= max(8, len(lst) // 2) and page >= 2:
                break
            if len(lst) < 20:
                break
            time.sleep(0.8)
        pub_today[shop_id] = rows_today
        print(f"  => {name} 今日发布记录 {len(rows_today)} 状态分布 {status_by_shop[shop_id]}")

    # published box by shop + today create (may undercount manual if create day earlier)
    print("\n=== 采集箱 published（gmtCreate 今天）===")
    box_today: dict[int, set] = {sid: set() for sid in SHOPS}
    for page in range(1, 40):
        body = qps_retry(
            lambda p=page: client.search_tiktok_box(page_no=p, page_size=100, status="published"),
            f"box_{page}",
        )
        lst = (body.get("data") or {}).get("detailList") or []
        if not lst:
            break
        page_today = 0
        for it in lst:
            dt = parse_gmt(str(it.get("gmtCreate") or ""))
            if not dt or dt < today0:
                continue
            page_today += 1
            did = str(it.get("collectBoxDetailId") or "")
            for s in it.get("collectBoxDetailShopList") or []:
                if not isinstance(s, dict):
                    continue
                try:
                    sid = int(s.get("shopId") or 0)
                except Exception:
                    continue
                if sid in box_today:
                    box_today[sid].add(did)
        print(f"  page {page}: n={len(lst)} today_on_page={page_today} first={lst[0].get('gmtCreate')} last={lst[-1].get('gmtCreate')}")
        oldest = parse_gmt(str(lst[-1].get("gmtCreate") or ""))
        if oldest and oldest < today0 and page_today == 0 and page >= 2:
            break
        if len(lst) < 100:
            break
        time.sleep(0.8)

    for sid, name in SHOPS.items():
        print(f"  {name} published箱今天创建 unique={len(box_today[sid])}")

    quota = json.loads((ROOT / "data/daily_quota.json").read_text(encoding="utf-8"))
    local = quota.get("2026-09-17") or {}
    print("\n=== 对比 ===")
    print("本地脚本配额:", local)
    for sid, name in SHOPS.items():
        print(
            f"{name}: 本地脚本={local.get(str(sid), local.get(sid))} | "
            f"妙手发布记录今日={len(pub_today[sid])} | "
            f"published箱今日创建={len(box_today[sid])}"
        )

    out = {
        "today": today0.isoformat(),
        "local_quota": local,
        "miaoshou_publish_records_today": {str(k): len(v) for k, v in pub_today.items()},
        "miaoshou_publish_status": {str(k): v for k, v in status_by_shop.items()},
        "miaoshou_published_box_created_today": {str(k): len(v) for k, v in box_today.items()},
        "publish_record_samples": {
            str(sid): [{k: it.get(k) for k in list(it.keys())[:25]} for it in pub_today[sid][:3]]
            for sid in SHOPS
        },
    }
    out_path = ROOT / "data/previews" / f"verify_today_publish_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
