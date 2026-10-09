"""从飞书「第一个未尝试」往下取一批 → 整批采集 → 统一认领改品上架到小赵。"""
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
from services.auto_batch import (
    batch_collect_rows,
    fresh_pending_rows,
    process_one_row,
    take_batch_from_start,
    write_collect_fail_record,
)
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable

SHOP_ID = 18545044
SHOP_NAME = "小赵1店"
DEFAULT_BATCH = 100


def main() -> int:
    batch_size = DEFAULT_BATCH
    start_from = 1  # 1-based，在「本批未尝试」中的起始序号
    if len(sys.argv) > 1:
        batch_size = int(sys.argv[1])
    if len(sys.argv) > 2:
        start_from = max(1, int(sys.argv[2]))

    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = dict(settings.get("feishu") or {})
    paths = settings.get("paths") or {}
    m = settings.get("miaoshou") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"feishu_zhao_batch_{stamp}.log"
    result_path = out_dir / f"feishu_zhao_batch_{stamp}.json"

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
    take_n = min(batch_size, remain)

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
    pending = fresh_pending_rows(records, fcfg)
    full = take_batch_from_start(pending, take_n)
    batch = full[start_from - 1 :]
    start_ts = batch[0].get("created_time") if batch else 0
    log(
        f"开始节点=第一个未尝试；窗口 {take_n} 条中从第 {start_from} 起实际跑 {len(batch)} 条 "
        f"（未尝试池 {len(pending)}，配额剩 {remain}）→ {SHOP_NAME}；"
        f"队首创建时间={start_ts}"
    )
    if not batch:
        log("没有未尝试链接，退出")
        return 1

    # ----- Phase A: 整批采集 -----
    log(f"=== PhaseA 批量采集 {len(batch)} 条 ===")
    collected = batch_collect_rows(client, batch, log, fetch_chunk=50)

    # 采集失败：立刻飞书写已尝试
    yes = fcfg.get("yes_value") or "是"
    no = fcfg.get("no_value") or "否"
    inbound_f = fcfg.get("inbound_status_field") or "入库状态"
    reason_f = fcfg.get("hold_reason_field") or "门禁原因"
    shop_f = fcfg.get("shop_field") or "分配店铺"
    for item in collected:
        if item.get("ok"):
            continue
        row = item["row"]
        err = str(item.get("error") or "采集失败")
        if len(err) > 200:
            err = err[:200] + "…"
        try:
            feishu.update_record(
                row["record_id"],
                {inbound_f: no, reason_f: err, shop_f: SHOP_NAME},
            )
        except Exception as e:
            log(f"飞书回写失败: {e}")
        write_collect_fail_record(
            feishu,
            fcfg,
            link=str(row.get("link") or item.get("link") or ""),
            reason=err,
            source_id=str(item.get("source_id") or ""),
            shop_name=SHOP_NAME,
            record_id=str(row.get("record_id") or ""),
            log=log,
        )

    successes = [x for x in collected if x.get("ok") and x.get("public_item")]
    log(f"=== PhaseB 统一认领改品上架：采集成功 {len(successes)} 条 ===")

    results: list[dict] = []
    ok = fail = held = 0
    for i, item in enumerate(successes, 1):
        if quota.remaining(SHOP_ID) <= 0:
            log("配额用尽，停止上架")
            break
        row = {**item["row"], "shop_id": SHOP_ID, "shop_name": SHOP_NAME}
        log(f"[pub {i}/{len(successes)}] {row['link'][:70]}")
        t0 = time.time()
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
            public_item=item["public_item"],
            source_id=str(item.get("source_id") or ""),
        )
        results.append(one)
        elapsed = time.time() - t0
        if one.get("held_in_box"):
            held += 1
            log(f"  留库 {elapsed:.1f}s")
        elif one.get("ok"):
            ok += 1
            log(f"  OK 已上架 {elapsed:.1f}s detail={one.get('detail_id')} ({ok})")
        else:
            fail += 1
            log(f"  FAIL {elapsed:.1f}s {str(one.get('error') or '')[:120]}")
        result_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(0.5)

    summary = {
        "batch_selected": len(batch),
        "collect_ok": len(successes),
        "collect_fail": len(batch) - len(successes),
        "publish_ok": ok,
        "publish_fail": fail,
        "held": held,
        "shop": SHOP_NAME,
        "quota_zhao": quota.count(SHOP_ID),
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"feishu_zhao_batch_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
