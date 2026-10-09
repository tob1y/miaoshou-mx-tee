# -*- coding: utf-8 -*-
import re
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.feishu_bitable import FeishuBitable
from services.link_resolve import extract_url_field

s = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
f = s["feishu"]
m = s["miaoshou"]

fail = FeishuBitable(
    f["app_id"], f["app_secret"], f["fail_collect_app_token"], f["fail_collect_table_id"]
)
recs = fail.list_records(page_size=500)

rows = []
for i, it in enumerate(recs, 1):
    fields = it.get("fields") or {}
    link = extract_url_field(fields.get("上传链接")) or ""
    sid = str(fields.get("货源ID") or "").strip()
    reason = str(fields.get("失败原因") or "")
    if not sid:
        mm = re.search(r"(1\d{15,})", reason)
        if mm:
            sid = mm.group(1)
    rows.append({"i": i, "sid": sid, "link": link, "reason": reason[:100]})

print("fail_table_total", len(rows))
nonempty = [r for r in rows if r["link"]]
print("nonempty_links", len(nonempty))
print("rows_i>=70:")
for r in rows:
    if r["i"] >= 70:
        print(r)

print("\nlast 12 nonempty:")
for r in nonempty[-12:]:
    print(f"i={r['i']} sid={r['sid'] or '-'} {r['link'][:55]} | {r['reason'][:55]}")

c = MiaoshouClient(
    str(m.get("app_key") or ""),
    str(m.get("app_secret") or ""),
    timeout=90,
    min_interval=1.4,
)

by_src: dict[str, list] = {}
status_c: Counter = Counter()
for page in range(1, 15):
    for attempt in range(6):
        try:
            body = c.assert_success(
                c.post(ep.PUBLIC_LIST, {"pageNo": page, "pageSize": 100}), "public"
            )
            break
        except Exception as e:
            if "Qps" in str(e) or "频率" in str(e):
                time.sleep(2.5 * (attempt + 1))
                continue
            raise
    else:
        print("page_fail", page)
        break
    data = body.get("data") or {}
    if page == 1:
        print("\npublic_total", data.get("total"))
    batch = data.get("detailList") or []
    if not batch:
        break
    for it in batch:
        st = str(it.get("status") or "")
        status_c[st] += 1
        sid = str(it.get("itemNum") or it.get("sourceItemId") or "")
        if not sid:
            continue
        by_src.setdefault(sid, []).append(
            {
                "status": st,
                "common_id": it.get("commonCollectBoxDetailId"),
                "title": str(it.get("title") or "")[:70],
                "group": str(it.get("commonCollectBoxGroupName") or ""),
                "gmt": it.get("gmtCreate"),
            }
        )
    if len(batch) < 100:
        break
    time.sleep(0.3)

print("public_status", dict(status_c), "unique_sources", len(by_src))

# Check rows >= 70 (and nearby 60+)
print("\n=== fail rows i>=60 vs public ===")
for r in rows:
    if r["i"] < 60 or not r["link"]:
        continue
    sid = r["sid"]
    hits = by_src.get(sid) or []
    if hits:
        best = sorted(hits, key=lambda x: 0 if x["status"] == "success" else 1)[0]
        print(
            f"i={r['i']} {sid} -> {best['status']} common={best['common_id']} "
            f"group={best['group'] or '-'} | {best['title']}"
        )
    else:
        # precise search by source keyword API if available
        print(f"i={r['i']} {sid or '?'} -> NOT_IN_FIRST_PAGES link={r['link'][:50]}")

# Precise lookup for i>=70 sids via public list filter if supported
print("\n=== precise lookup for i>=70 ===")
for r in rows:
    if r["i"] < 70:
        continue
    sid = r["sid"]
    if not sid:
        print(f"i={r['i']} no source id, link={r['link']}")
        continue
    found = by_src.get(sid)
    if found:
        for h in found:
            print(f"i={r['i']} FOUND {sid} status={h['status']} common={h['common_id']}")
        continue
    # try search with keyword in PUBLIC_LIST filter
    try:
        body = c.assert_success(
            c.post(
                ep.PUBLIC_LIST,
                {
                    "pageNo": 1,
                    "pageSize": 20,
                    "filter": {"sourceItemIdKeyword": sid},
                },
            ),
            "public_by_sid",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            print(f"i={r['i']} {sid} PRECISE_EMPTY")
        for it in batch:
            print(
                f"i={r['i']} PRECISE {sid} status={it.get('status')} "
                f"common={it.get('commonCollectBoxDetailId')} "
                f"item={it.get('itemNum')} title={str(it.get('title') or '')[:50]}"
            )
    except Exception as e:
        print(f"i={r['i']} {sid} precise_err {e}")
    time.sleep(1.2)

# Summary: how many fail-table sources now have success in public
all_sids = sorted({r["sid"] for r in nonempty if r["sid"]})
ok = failn = miss = other = 0
print("\n=== all fail-table sources with sid ===")
for sid in all_sids:
    hits = by_src.get(sid) or []
    if not hits:
        # precise
        try:
            body = c.assert_success(
                c.post(
                    ep.PUBLIC_LIST,
                    {"pageNo": 1, "pageSize": 10, "filter": {"sourceItemIdKeyword": sid}},
                ),
                "pub",
            )
            hits = []
            for it in (body.get("data") or {}).get("detailList") or []:
                hits.append(
                    {
                        "status": str(it.get("status") or ""),
                        "common_id": it.get("commonCollectBoxDetailId"),
                        "title": str(it.get("title") or "")[:60],
                        "group": str(it.get("commonCollectBoxGroupName") or ""),
                    }
                )
            time.sleep(1.0)
        except Exception:
            hits = []
    if not hits:
        miss += 1
        print(f"MISS {sid}")
        continue
    sts = {h["status"] for h in hits}
    if "success" in sts:
        ok += 1
        h = next(x for x in hits if x["status"] == "success")
        print(f"OK  {sid} common={h['common_id']} group={h.get('group') or '-'} | {h['title']}")
    elif "fail" in sts:
        failn += 1
        print(f"FAIL {sid}")
    else:
        other += 1
        print(f"OTHER {sid} {sts}")

print(f"\nSUMMARY sources_with_sid={len(all_sids)} success={ok} fail={failn} other={other} miss={miss}")
