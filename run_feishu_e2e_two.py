"""E2E: Feishu 2 links → Miaoshou fetch → split shops → claim/edit/publish → write back."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient
from services.full_pipeline import list_public_items, run_full_pipeline

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
        raise RuntimeError(f"feishu token fail: {tok}")
    return tok["tenant_access_token"]


def extract_link(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return str(val.get("link") or val.get("text") or "").strip()
    if isinstance(val, list) and val:
        return extract_link(val[0])
    return str(val).strip()


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = settings.get("feishu") or {}
    m = settings.get("miaoshou") or {}
    app_token = fcfg["bitable_app_token"]
    table_id = fcfg["bitable_table_id"]
    link_field = fcfg.get("link_field") or "上传链接"
    inbound_status = fcfg.get("inbound_status_field") or "入库状态"
    inbound_time = fcfg.get("inbound_time_field") or "入库时间"
    publish_status = fcfg.get("publish_status_field") or "上架状态"
    publish_time = fcfg.get("publish_time_field") or "上架时间"

    print("0) 读飞书字段选项…", flush=True)
    token = feishu_token(fcfg["app_id"], fcfg["app_secret"])
    auth = {"Authorization": f"Bearer {token}"}
    _, fields_body = http_json(
        "GET",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        headers=auth,
    )
    print(json.dumps(fields_body, ensure_ascii=False, indent=2)[:3000], flush=True)

    _, recs_body = http_json(
        "GET",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=50",
        headers=auth,
    )
    items = ((recs_body.get("data") or {}).get("items") or [])
    pending = []
    for it in items:
        fields = it.get("fields") or {}
        link = extract_link(fields.get(link_field))
        st = fields.get(inbound_status)
        if link and not st:
            pending.append({"record_id": it["record_id"], "link": link, "fields": fields})
    print(f"\n待处理 {len(pending)} 条:", flush=True)
    for p in pending:
        print(f"  {p['record_id']} {p['link']}", flush=True)
    if len(pending) < 1:
        print("没有待处理链接", flush=True)
        return 1

    # take up to 2 for this test, assign 1 each shop
    batch = pending[:2]
    while len(batch) < 2 and len(pending) > len(batch):
        batch.append(pending[len(batch)])
    assignments = []
    for i, row in enumerate(batch):
        shop = SHOPS[i % len(SHOPS)]
        assignments.append({**row, **shop})
        print(f"分配: {row['link'][:60]} → {shop['shop_name']}", flush=True)

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=1.5,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    pub_base = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")

    links = [a["link"] for a in assignments]
    print("\n1) 妙手采集 fetch_item…", flush=True)
    before_ids = {
        int(it.get("commonCollectBoxDetailId"))
        for it in list_public_items(client, page_size=100)
        if it.get("commonCollectBoxDetailId") is not None
    }
    try:
        fetch_resp = client.fetch_item(links)
        print("fetch ok:", json.dumps(fetch_resp, ensure_ascii=False)[:1500], flush=True)
    except Exception as e:
        print("fetch ERR:", e, flush=True)
        # still try wait in case async already kicked
        fetch_resp = {"error": str(e)}

    print("\n2) 等待进入公共采集箱…", flush=True)
    matched: dict[str, dict] = {}
    deadline = time.time() + 180
    while time.time() < deadline and len(matched) < len(links):
        public = list_public_items(client, page_size=100)
        # newest first
        public_sorted = sorted(public, key=lambda x: str(x.get("gmtCreate") or ""), reverse=True)
        newish = [
            it
            for it in public_sorted
            if int(it.get("commonCollectBoxDetailId") or 0) not in before_ids
            or str(it.get("status") or "").lower() in ("success", "collecting", "processing", "")
        ]
        print(
            f"  public={len(public)} new_candidates={len(newish)} matched={len(matched)}",
            flush=True,
        )
        # Prefer matching by source link / item url fields if present
        for it in public_sorted[:30]:
            cid = int(it.get("commonCollectBoxDetailId") or 0)
            blob = json.dumps(it, ensure_ascii=False)
            for a in assignments:
                if a["link"] in matched:
                    continue
                # short-link tip often not stored; fallback later by recency
                if a["link"] in blob or a["link"].rstrip("/") in blob:
                    matched[a["link"]] = it
                    print(f"  matched by link blob: {a['link'][:40]} -> {cid}", flush=True)
        if len(matched) < len(links):
            # fallback: take newest success items not in before_ids
            fresh = [
                it
                for it in public_sorted
                if int(it.get("commonCollectBoxDetailId") or 0) not in before_ids
                and str(it.get("status") or "").lower() == "success"
            ]
            if len(fresh) >= len(links) - len(matched):
                unused = [it for it in fresh if it not in matched.values()]
                for a in assignments:
                    if a["link"] in matched:
                        continue
                    if unused:
                        it = unused.pop(0)
                        matched[a["link"]] = it
                        print(
                            f"  matched by recency: {a['link'][:40]} -> "
                            f"{it.get('commonCollectBoxDetailId')} {(it.get('title') or '')[:40]}",
                            flush=True,
                        )
        if len(matched) >= len(links):
            break
        time.sleep(8)

    if len(matched) < len(links):
        print("采集未全部就绪，当前 matched=", list(matched.keys()), flush=True)
        # continue with whatever we have

    # write inbound status
    now_ms = int(time.time() * 1000)
    print("\n3) 回写入库状态…", flush=True)
    for a in assignments:
        it = matched.get(a["link"])
        fields_upd = {
            inbound_time: now_ms,
        }
        # 飞书单选选项：是 / 否
        fields_upd[inbound_status] = "是" if it else "否"
        code, upd = http_json(
            "PUT",
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{a['record_id']}",
            {"fields": fields_upd},
            headers=auth,
        )
        print(f"  inbound write {a['record_id']}: http={code} code={upd.get('code')} msg={upd.get('msg')}", flush=True)
        if upd.get("code") not in (0, None):
            print("   body", json.dumps(upd, ensure_ascii=False)[:500], flush=True)

    # run pipelines per shop
    print("\n4) 认领改品上架…", flush=True)
    results = []
    for a in assignments:
        it = matched.get(a["link"])
        if not it:
            results.append({**a, "ok": False, "error": "采集未入库"})
            continue
        pub_cfg = dict(pub_base)
        pub_cfg["shop_id"] = a["shop_id"]
        pub_cfg["shop_name"] = a["shop_name"]
        print(f"\n=== {a['shop_name']} detail={it.get('commonCollectBoxDetailId')} ===", flush=True)
        try:
            pr = run_full_pipeline(
                client,
                items=[it],
                shop_id=a["shop_id"],
                shop_name=a["shop_name"],
                scheme=scheme,
                publish_cfg=pub_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                out_dir=out_dir,
                do_publish=True,
                log=lambda m: print(m, flush=True),
            )
            edit_ok = any(r.get("ok") for r in pr.edited)
            pub_ok = False
            if isinstance(pr.publish, dict):
                calls = pr.publish.get("publish_calls") or []
                pub_ok = bool(calls) and all(c.get("ok") is not False for c in calls) and pr.publish.get("ok") is not False
                if "error" in pr.publish and not calls:
                    pub_ok = False
                if calls:
                    pub_ok = any(c.get("ok", True) for c in calls)
            results.append(
                {
                    **a,
                    "ok": edit_ok and pub_ok,
                    "edit_ok": edit_ok,
                    "pub_ok": pub_ok,
                    "common_id": it.get("commonCollectBoxDetailId"),
                    "edited": pr.edited,
                    "publish": pr.publish,
                    "report": pr.report_path,
                }
            )
        except Exception as e:
            print(f"PIPELINE FAIL: {e}", flush=True)
            results.append({**a, "ok": False, "error": str(e)})

    print("\n5) 回写上架状态…", flush=True)
    now_ms = int(time.time() * 1000)
    for r in results:
        fields_upd = {publish_time: now_ms}
        fields_upd[publish_status] = "是" if (r.get("ok") or r.get("pub_ok")) else "否"
        code, upd = http_json(
            "PUT",
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{r['record_id']}",
            {"fields": fields_upd},
            headers=auth,
        )
        print(
            f"  publish write {r['record_id']} {r.get('shop_name')}: "
            f"http={code} code={upd.get('code')} msg={upd.get('msg')} ok={r.get('ok')}",
            flush=True,
        )
        if upd.get("code") not in (0, None):
            print("   body", json.dumps(upd, ensure_ascii=False)[:800], flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = out_dir / f"feishu_e2e_{stamp}.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n完成报告: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
