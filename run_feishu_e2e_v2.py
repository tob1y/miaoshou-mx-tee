"""Retry E2E with sourceItemId matching (no recency guess)."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.full_pipeline import list_public_items, run_full_pipeline, qps_retry

SHOPS = [
    {"shop_id": 18545217, "shop_name": "小韩1店"},
    {"shop_id": 18545044, "shop_name": "小赵1店"},
]


def http_json(method: str, url: str, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            j = json.loads(raw)
        except Exception:
            j = {"raw": raw}
        return e.code, j


def feishu_token(app_id: str, app_secret: str) -> str:
    _, tok = http_json(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if tok.get("code") != 0:
        raise RuntimeError(str(tok))
    return tok["tenant_access_token"]


def extract_link(val) -> str:
    if isinstance(val, dict):
        return str(val.get("link") or val.get("text") or "").strip()
    if isinstance(val, str):
        return val.strip()
    return ""


def resolve_source_id(link: str) -> tuple[str, str]:
    sid = ""
    m = re.search(r"/(?:pdp|product)/(\d{12,22})(?:/|$|\?)", link)
    if m:
        return m.group(1), link
    r = requests.get(link, timeout=25, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    final = r.url
    m = re.search(r"/(?:pdp|product)/(\d{12,22})(?:/|$|\?)", final)
    if m:
        sid = m.group(1)
    return sid, final


def deep_find_map(obj):
    if isinstance(obj, dict):
        if "sourceItemIdAndDetailIdMap" in obj and isinstance(obj["sourceItemIdAndDetailIdMap"], dict):
            return obj["sourceItemIdAndDetailIdMap"]
        for v in obj.values():
            found = deep_find_map(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = deep_find_map(v)
            if found:
                return found
    return {}


def find_public_by_source(client: MiaoshouClient, source_id: str) -> dict | None:
    # keyword search on public list if supported; else scan pages
    for page in range(1, 6):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(
                    ep.PUBLIC_LIST,
                    {
                        "pageNo": p,
                        "pageSize": 100,
                        "filter": {"sourceItemIdKeyword": str(source_id)},
                    },
                ),
                "公共箱按源ID",
            ),
            "public_kw",
        )
        lst = (body.get("data") or {}).get("detailList") or []
        for it in lst:
            blob = json.dumps(it, ensure_ascii=False)
            if source_id in blob or str(it.get("itemNum") or "") == source_id or str(it.get("sourceItemId") or "") == source_id:
                return it
        if not lst:
            break
        time.sleep(0.5)

    # fallback scan without filter
    for page in range(1, 4):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "公共箱",
            ),
            "public_scan",
        )
        for it in (body.get("data") or {}).get("detailList") or []:
            if source_id in json.dumps(it, ensure_ascii=False):
                return it
        time.sleep(0.4)
    return None


def update_record(auth, app_token, table_id, record_id, fields):
    return http_json(
        "PUT",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        {"fields": fields},
        headers=auth,
    )


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = settings["feishu"]
    m = settings["miaoshou"]
    app_token, table_id = fcfg["bitable_app_token"], fcfg["bitable_table_id"]
    token = feishu_token(fcfg["app_id"], fcfg["app_secret"])
    auth = {"Authorization": f"Bearer {token}"}

    _, recs = http_json(
        "GET",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=50",
        headers=auth,
    )
    rows = []
    for it in (recs.get("data") or {}).get("items") or []:
        link = extract_link((it.get("fields") or {}).get("上传链接"))
        if not link:
            continue
        sid, final = resolve_source_id(link)
        rows.append({"record_id": it["record_id"], "link": link, "source_id": sid, "final_url": final})
        print(f"row {it['record_id']} sid={sid} link={link}", flush=True)

    if len(rows) < 2:
        print("需要至少2条链接", flush=True)
        return 1

    # assign 1 each
    for i, row in enumerate(rows[:2]):
        row.update(SHOPS[i % 2])
        print(f"分配 {row['source_id']} → {row['shop_name']}", flush=True)

    client = MiaoshouClient(
        app_key=m["app_key"],
        app_secret=m["app_secret"],
        base_url=m.get("base_url", "https://openapi-erp.91miaoshou.com"),
        timeout=120,
        min_interval=1.5,
        max_retries=2,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    pub_base = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")

    print("\n1) 逐条 fetch_item…", flush=True)
    for row in rows[:2]:
        # prefer full product URL for collect
        collect_link = row["final_url"].split("?")[0] if row.get("final_url") else row["link"]
        if "/pdp/" not in collect_link and row["source_id"]:
            collect_link = f"https://shop.tiktok.com/mx/pdp/{row['source_id']}"
        print(f"  collect {collect_link}", flush=True)
        try:
            resp = client.fetch_item([collect_link])
            mapping = deep_find_map(resp)
            print(f"  fetch ok map={mapping}", flush=True)
            row["fetch_map"] = mapping
            if row["source_id"] and row["source_id"] in mapping:
                row["public_id"] = int(mapping[row["source_id"]])
        except Exception as e:
            print(f"  fetch warn: {e}", flush=True)
            row["fetch_error"] = str(e)
        time.sleep(2)

    print("\n2) 按 sourceItemId 匹配公共箱…", flush=True)
    for row in rows[:2]:
        if row.get("public_id"):
            # still need full item object
            it = find_public_by_source(client, row["source_id"])
            row["public_item"] = it
            print(f"  {row['source_id']} public_id={row.get('public_id')} item={'yes' if it else 'no'}", flush=True)
            continue
        found = None
        for attempt in range(1, 25):
            found = find_public_by_source(client, row["source_id"])
            if found and str(found.get("status") or "").lower() == "success":
                break
            print(f"  wait {row['source_id']} #{attempt} found={bool(found)} status={(found or {}).get('status')}", flush=True)
            time.sleep(6)
        row["public_item"] = found
        if found:
            row["public_id"] = found.get("commonCollectBoxDetailId")
            print(f"  OK {row['source_id']} → {row['public_id']} {(found.get('title') or '')[:50]}", flush=True)
        else:
            print(f"  FAIL match {row['source_id']}", flush=True)

    now_ms = int(time.time() * 1000)
    print("\n3) 回写入库状态…", flush=True)
    for row in rows[:2]:
        ok = bool(row.get("public_item"))
        code, upd = update_record(
            auth,
            app_token,
            table_id,
            row["record_id"],
            {"入库状态": "是" if ok else "否", "入库时间": now_ms},
        )
        print(f"  inbound {row['record_id']} ok={ok} code={upd.get('code')} msg={upd.get('msg')}", flush=True)

    print("\n4) 认领改品上架…", flush=True)
    results = []
    for row in rows[:2]:
        it = row.get("public_item")
        if not it:
            results.append({**row, "ok": False, "error": "无公共箱商品"})
            continue
        pub_cfg = dict(pub_base)
        pub_cfg["shop_id"] = row["shop_id"]
        pub_cfg["shop_name"] = row["shop_name"]
        print(f"\n=== {row['shop_name']} source={row['source_id']} common={it.get('commonCollectBoxDetailId')} ===", flush=True)
        try:
            pr = run_full_pipeline(
                client,
                items=[it],
                shop_id=row["shop_id"],
                shop_name=row["shop_name"],
                scheme=scheme,
                publish_cfg=pub_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                out_dir=out_dir,
                do_publish=True,
                log=lambda msg: print(msg, flush=True),
            )
            edit_ok = any(r.get("ok") for r in pr.edited)
            calls = (pr.publish or {}).get("publish_calls") if isinstance(pr.publish, dict) else None
            pub_ok = bool(calls) and not (isinstance(pr.publish, dict) and pr.publish.get("error"))
            if isinstance(pr.publish, dict) and pr.publish.get("ok") is False:
                pub_ok = False
            results.append(
                {
                    "record_id": row["record_id"],
                    "shop_name": row["shop_name"],
                    "source_id": row["source_id"],
                    "link": row["link"],
                    "edit_ok": edit_ok,
                    "pub_ok": pub_ok,
                    "ok": edit_ok and pub_ok,
                    "edited": pr.edited,
                    "publish": pr.publish,
                    "report": pr.report_path,
                }
            )
        except Exception as e:
            print(f"PIPELINE FAIL: {e}", flush=True)
            results.append(
                {
                    "record_id": row["record_id"],
                    "shop_name": row["shop_name"],
                    "source_id": row["source_id"],
                    "ok": False,
                    "error": str(e),
                }
            )

    print("\n5) 回写上架状态…", flush=True)
    now_ms = int(time.time() * 1000)
    for r in results:
        ok = bool(r.get("ok") or r.get("pub_ok"))
        code, upd = update_record(
            auth,
            app_token,
            table_id,
            r["record_id"],
            {"上架状态": "是" if ok else "否", "上架时间": now_ms},
        )
        print(
            f"  publish {r['record_id']} {r.get('shop_name')} ok={ok} "
            f"code={upd.get('code')} msg={upd.get('msg')}",
            flush=True,
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"feishu_e2e_v2_{stamp}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n完成: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
