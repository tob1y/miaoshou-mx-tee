"""快速重试：用已解析的 source_id，跳过短链；采集最多等 ~16s。"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.auto_batch import claim_to_detail, edit_and_publish_one, ensure_collected
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable

SHOP_MAP = {"小韩1店": 18545217, "小赵1店": 18545044}


def source_from_row(r: dict) -> str:
    if r.get("source_id"):
        return str(r["source_id"])
    err = str(r.get("error") or "")
    m = re.search(r"(?:success:\s*|/pdp/)(\d{12,22})", err)
    return m.group(1) if m else ""


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = dict(settings.get("feishu") or {})
    paths = settings.get("paths") or {}
    m = settings.get("miaoshou") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / "feishu_100_retry_fast.log"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    batch = json.loads((out_dir / "feishu_100_20260914_161126.json").read_text(encoding="utf-8"))
    prev = out_dir / "feishu_100_retry19_20260914_172305.json"
    rescued = set()
    if prev.exists():
        rescued = {
            r["record_id"]
            for r in json.loads(prev.read_text(encoding="utf-8"))
            if r.get("ok") or r.get("held_in_box")
        }
    fails = [
        r
        for r in batch
        if not r.get("ok") and not r.get("held_in_box") and r.get("record_id") not in rescued
    ]
    log(f"快速重试 {len(fails)} 条（已跳过短链解析）")

    feishu = FeishuBitable(
        app_id=str(fcfg["app_id"]),
        app_secret=str(fcfg["app_secret"]),
        app_token=str(fcfg["bitable_app_token"]),
        table_id=str(fcfg["bitable_table_id"]),
    )
    quota = DailyQuota(ROOT / "data" / "daily_quota.json", int(fcfg.get("daily_limit_per_shop") or 300))
    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 90),
        min_interval=1.2,
        max_retries=3,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]

    yes = fcfg.get("yes_value") or "是"
    no = fcfg.get("no_value") or "否"
    inbound_f = fcfg.get("inbound_status_field") or "入库状态"
    inbound_t = fcfg.get("inbound_time_field") or "入库时间"
    publish_f = fcfg.get("publish_status_field") or "上架状态"
    publish_t = fcfg.get("publish_time_field") or "上架时间"
    shop_f = fcfg.get("shop_field") or "分配店铺"

    results = []
    ok = fail = held = 0
    result_path = out_dir / f"feishu_100_retry_fast_{stamp}.json"
    for i, raw in enumerate(fails, 1):
        t0 = time.time()
        shop_name = raw.get("shop_name") or ""
        shop_id = int(raw.get("shop_id") or SHOP_MAP.get(shop_name) or 0)
        source_id = source_from_row(raw)
        collect_url = f"https://shop.tiktok.com/mx/pdp/{source_id}" if source_id else ""
        log(f"[{i}/{len(fails)}] → {shop_name} source={source_id or '?'} {raw.get('link','')[:50]}")
        row_out = {
            "record_id": raw["record_id"],
            "link": raw.get("link"),
            "shop_id": shop_id,
            "shop_name": shop_name,
            "source_id": source_id,
            "ok": False,
        }
        now_ms = int(time.time() * 1000)
        try:
            if not source_id:
                raise MiaoshouError("无 source_id，短链也无法解析")
            public_item = ensure_collected(client, source_id, collect_url, log)
            try:
                feishu.update_record(
                    raw["record_id"],
                    {inbound_f: yes, inbound_t: now_ms, shop_f: shop_name},
                )
            except Exception:
                feishu.update_record(raw["record_id"], {inbound_f: yes, inbound_t: now_ms})
            detail_id = claim_to_detail(client, public_item, shop_id, source_id, log)
            row_out["detail_id"] = detail_id
            ep = edit_and_publish_one(
                client,
                detail_id=detail_id,
                shop_id=shop_id,
                shop_name=shop_name,
                scheme=scheme,
                publish_cfg=publish_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                log=log,
            )
            row_out.update(ep)
            feishu.update_record(
                raw["record_id"],
                {publish_f: yes if ep.get("ok") else no, publish_t: int(time.time() * 1000)},
            )
            if ep.get("ok"):
                quota.add(shop_id, 1)
                ok += 1
                log(f"  OK {time.time()-t0:.1f}s")
            elif ep.get("held_in_box"):
                held += 1
                log(f"  留库 {time.time()-t0:.1f}s")
            else:
                fail += 1
                log(f"  FAIL publish {time.time()-t0:.1f}s")
        except Exception as e:
            fail += 1
            row_out["error"] = str(e)
            log(f"  FAIL {time.time()-t0:.1f}s {e}")
            try:
                feishu.update_record(
                    raw["record_id"],
                    {
                        inbound_f: no,
                        inbound_t: now_ms,
                        publish_f: no,
                        publish_t: int(time.time() * 1000),
                    },
                )
            except Exception:
                pass
        results.append(row_out)
        result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        time.sleep(0.8)

    summary = {"total": len(fails), "ok": ok, "fail": fail, "held": held}
    log(f"=== 快速重试结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"feishu_100_retry_fast_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
