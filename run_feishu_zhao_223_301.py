"""飞书台账序号 223–301（按当前列表顺序）→ 全部小赵1店认领/改品/上架。"""
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
from services.auto_batch import extract_url_field, process_one_row
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable

SEQ_START = 223  # 1-based inclusive
SEQ_END = 301
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
    log_path = out_dir / "feishu_zhao_223_301.log"
    result_path = out_dir / f"feishu_zhao_223_301_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    link_f = fcfg.get("link_field") or "上传链接"
    pub_f = fcfg.get("publish_status_field") or "上架状态"
    yes = fcfg.get("yes_value") or "是"

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
    if len(records) < SEQ_END:
        log(f"WARNING: 台账仅 {len(records)} 条，不足 {SEQ_END}")
    slice_recs = records[SEQ_START - 1 : SEQ_END]

    assigned = []
    for i, it in enumerate(slice_recs, start=SEQ_START):
        fields = it.get("fields") or {}
        link = extract_url_field(fields.get(link_f))
        if not link:
            log(f"跳过 #{i}：无链接")
            continue
        st = fields.get(pub_f)
        if st == yes or (isinstance(st, dict) and st.get("name") == yes):
            log(f"跳过 #{i}：已上架")
            continue
        assigned.append(
            {
                "record_id": it.get("record_id") or it.get("id"),
                "link": link,
                "fields": fields,
                "seq": i,
                "shop_id": SHOP_ID,
                "shop_name": SHOP_NAME,
            }
        )

    take_n = min(len(assigned), remain)
    if take_n < len(assigned):
        log(f"WARNING: 配额不足，{len(assigned)}→{take_n}")
        assigned = assigned[:take_n]

    log(f"序号 {SEQ_START}-{SEQ_END} 待跑 {len(assigned)} 条 → 全部 {SHOP_NAME}（剩余配额 {remain}）")
    (out_dir / f"feishu_zhao_223_301_plan_{stamp}.json").write_text(
        json.dumps(
            [{"seq": r["seq"], "record_id": r["record_id"], "link": r["link"]} for r in assigned],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    results = []
    ok = fail = held = 0
    for i, row in enumerate(assigned, 1):
        t0 = time.time()
        log(f"[{i}/{len(assigned)}] #{row['seq']} → {SHOP_NAME} {row['link'][:70]}")
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
        one["seq"] = row["seq"]
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
        "seq": f"{SEQ_START}-{SEQ_END}",
        "quota_zhao": quota.count(SHOP_ID),
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"feishu_zhao_223_301_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
