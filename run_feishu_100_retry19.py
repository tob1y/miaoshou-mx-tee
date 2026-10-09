"""重试飞书100批失败的19条（不含留库）。"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import process_one_row
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable

SHOP_MAP = {
    "小韩1店": 18545217,
    "小赵1店": 18545044,
}


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = dict(settings.get("feishu") or {})
    paths = settings.get("paths") or {}
    m = settings.get("miaoshou") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / "feishu_100_retry19.log"
    result_path = out_dir / f"feishu_100_retry19_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    pending_file = out_dir / "feishu_100_待重试.json"
    if not pending_file.exists():
        # fallback: rebuild from batch results
        batch = out_dir / "feishu_100_20260914_161126.json"
        rows = json.loads(batch.read_text(encoding="utf-8"))
        fails = [r for r in rows if not r.get("ok") and not r.get("held_in_box")]
    else:
        data = json.loads(pending_file.read_text(encoding="utf-8"))
        fails = data.get("fail_rows") or []
        # enrich shop_id from batch if missing
        batch = out_dir / "feishu_100_20260914_161126.json"
        if batch.exists():
            by_id = {
                r["record_id"]: r
                for r in json.loads(batch.read_text(encoding="utf-8"))
                if r.get("record_id")
            }
            enriched = []
            for f in fails:
                full = by_id.get(f["record_id"], {})
                enriched.append({**full, **f})
            fails = enriched

    log(f"重试失败 {len(fails)} 条")
    if not fails:
        log("没有失败条目")
        return 0

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
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=2.0,
        max_retries=4,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]

    results = []
    ok = fail = held = 0
    for i, raw in enumerate(fails, 1):
        shop_name = raw.get("shop_name") or ""
        shop_id = int(raw.get("shop_id") or SHOP_MAP.get(shop_name) or 0)
        row = {
            "record_id": raw["record_id"],
            "link": raw["link"],
            "shop_id": shop_id,
            "shop_name": shop_name,
            "fields": raw.get("fields") or {},
        }
        log(f"[{i}/{len(fails)}] → {shop_name} {row['link'][:70]}")
        # SSL 失败多等一会再解析
        time.sleep(2.0)
        one = process_one_row(
            client,
            feishu,
            row,
            fcfg=fcfg,
            scheme=scheme,
            publish_cfg=publish_cfg,
            blank_dir=blank,
            fill_urls=fill_urls,
            quota=quota,
            log=log,
        )
        results.append(one)
        if one.get("held_in_box"):
            held += 1
            log("  留库不上架")
        elif one.get("ok"):
            ok += 1
            log("  OK 已上架")
        else:
            fail += 1
            log(f"  FAIL {str(one.get('error') or '')[:140]}")
        result_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(2.0)

    summary = {"total": len(fails), "ok": ok, "fail": fail, "held": held}
    log(f"=== 重试结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"feishu_100_retry19_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    still = [r for r in results if not r.get("ok") and not r.get("held_in_box")]
    (out_dir / "feishu_100_retry19_仍失败.json").write_text(
        json.dumps(still, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
