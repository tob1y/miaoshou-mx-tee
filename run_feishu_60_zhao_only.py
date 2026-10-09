"""飞书待处理取 60 条，全部认领/改品/上架到小赵1店。"""
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
from services.auto_batch import pending_rows, process_one_row
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable

TAKE = 60
SHOP_ID = 18545044
SHOP_NAME = "小赵1店"


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = dict(settings.get("feishu") or {})
    paths = settings.get("paths") or {}
    m = settings.get("miaoshou") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / "feishu_60_zhao_only.log"
    result_path = out_dir / f"feishu_60_zhao_only_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    feishu = FeishuBitable(
        app_id=str(fcfg["app_id"]),
        app_secret=str(fcfg["app_secret"]),
        app_token=str(fcfg["bitable_app_token"]),
        table_id=str(fcfg["bitable_table_id"]),
    )
    quota = DailyQuota(
        ROOT / "data" / "daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )
    remain = quota.remaining(SHOP_ID)
    if remain <= 0:
        log("小赵今日配额已满，退出")
        return 1

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=1.2,
        max_retries=3,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]

    records = feishu.list_records(page_size=500)
    pending = pending_rows(records, fcfg)
    take_n = min(TAKE, len(pending), remain)
    take = pending[:take_n]
    log(f"待处理 {len(pending)}，取 {take_n} 条 → 全部 {SHOP_NAME}（今日剩余配额 {remain}）")
    if take_n < TAKE:
        log(f"WARNING: 实际可跑 {take_n}（不足 {TAKE}）")

    assigned = [{**r, "shop_id": SHOP_ID, "shop_name": SHOP_NAME} for r in take]
    (out_dir / f"feishu_60_zhao_plan_{stamp}.json").write_text(
        json.dumps(
            [{"record_id": r["record_id"], "link": r["link"], "shop_name": SHOP_NAME} for r in assigned],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    results = []
    ok = fail = held = 0
    for i, row in enumerate(assigned, 1):
        t0 = time.time()
        log(f"[{i}/{len(assigned)}] → {SHOP_NAME} {row['link'][:70]}")
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
        elapsed = time.time() - t0
        if one.get("held_in_box"):
            held += 1
            log(f"  留库不上架 {elapsed:.1f}s")
        elif one.get("ok"):
            ok += 1
            log(f"  OK 已上架 {elapsed:.1f}s")
        else:
            fail += 1
            log(f"  FAIL {elapsed:.1f}s {str(one.get('error') or '')[:120]}")
        result_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(0.8)

    summary = {
        "total": len(assigned),
        "ok": ok,
        "fail": fail,
        "held": held,
        "shop": SHOP_NAME,
        "quota_zhao": quota.count(SHOP_ID),
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"feishu_60_zhao_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
