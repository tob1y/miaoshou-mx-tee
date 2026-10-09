# -*- coding: utf-8 -*-
"""预检可上架清单：前 300 条上小韩，其余上小赵。"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one
from services.daily_quota import DailyQuota

HAN_ID = 18545217
HAN_NAME = "小韩1店"
ZHAO_ID = 18545044
ZHAO_NAME = "小赵1店"
HAN_N = 300
SOURCE = ROOT / "data/previews/public_can_publish_20260918_190904.json"


def already_published_sids() -> set[str]:
    """上一轮已真正上架的货源（含日志变量报错但配额已加的）。"""
    done: set[str] = set()
    for log in (ROOT / "data/previews").glob("split_publish_han300_*.log"):
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        sid = ""
        for line in lines:
            m = re.search(r"sid=(\d+)", line)
            if m and "common=" in line:
                sid = m.group(1)
                continue
            if sid and ("OK 已上架" in line or "name 'detail_id' is not defined" in line):
                done.add(sid)
                sid = ""
    return done


def main() -> int:
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"split_publish_han300_{stamp}.log"
    result_path = out_dir / f"split_publish_han300_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    rows = [r for r in json.loads(SOURCE.read_text(encoding="utf-8")) if r.get("can_publish")]
    # 去重 sid，保序
    uniq: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        sid = str(r.get("sid") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        uniq.append(r)
    done = already_published_sids()
    han_rows = [r for r in uniq[:HAN_N] if str(r.get("sid") or "") not in done]
    zhao_rows = [r for r in uniq[HAN_N:] if str(r.get("sid") or "") not in done]
    plan = [(HAN_ID, HAN_NAME, han_rows), (ZHAO_ID, ZHAO_NAME, zhao_rows)]

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

    log(
        f"可上架去重 {len(uniq)}：{HAN_NAME} {len(han_rows)}（配额剩 {quota.remaining(HAN_ID)}）"
        f"；{ZHAO_NAME} {len(zhao_rows)}（配额剩 {quota.remaining(ZHAO_ID)}）；已上架跳过 {len(done)}"
    )

    results: list[dict] = []
    stats = {HAN_ID: {"ok": 0, "held": 0, "fail": 0}, ZHAO_ID: {"ok": 0, "held": 0, "fail": 0}}

    for shop_id, shop_name, batch in plan:
        log(f"=== {shop_name} {len(batch)} 条 ===")
        for i, row in enumerate(batch, 1):
            if quota.remaining(shop_id) <= 0:
                log(f"{shop_name} 配额用尽，剩余 {len(batch) - i + 1} 条未跑")
                break
            sid = str(row["sid"])
            common_id = int(row["common_id"])
            log(f"[{shop_name} {i}/{len(batch)}] common={common_id} sid={sid}")
            t0 = time.time()
            one: dict = {
                "shop_id": shop_id,
                "shop_name": shop_name,
                "source_id": sid,
                "common_id": common_id,
                "ok": False,
            }
            try:
                public_item = {
                    "commonCollectBoxDetailId": common_id,
                    "itemNum": sid,
                    "status": "success",
                }

                def _once() -> dict:
                    detail_id = claim_to_detail(client, public_item, shop_id, sid, log)
                    one["detail_id"] = detail_id
                    return edit_and_publish_one(
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

                try:
                    ep_res = _once()
                except Exception as e1:
                    if "产品数据发生变动" not in str(e1):
                        raise
                    log("  改品并发，重试一次")
                    time.sleep(2.5)
                    ep_res = _once()
                blob = json.dumps(ep_res, ensure_ascii=False, default=str)
                if (not ep_res.get("ok")) and (not ep_res.get("held_in_box")) and "产品数据发生变动" in blob:
                    log("  改品并发，再试一次")
                    time.sleep(2.5)
                    ep_res = _once()
                one.update({k: v for k, v in ep_res.items() if k != "prep"})
                if ep_res.get("held_in_box"):
                    stats[shop_id]["held"] += 1
                    log(f"  留库 {time.time()-t0:.1f}s")
                elif ep_res.get("ok"):
                    stats[shop_id]["ok"] += 1
                    quota.add(shop_id, 1)
                    log(f"  OK 已上架 {time.time()-t0:.1f}s detail={one.get('detail_id')} ({stats[shop_id]['ok']})")
                else:
                    stats[shop_id]["fail"] += 1
                    log(f"  FAIL {time.time()-t0:.1f}s")
            except Exception as e:
                stats[shop_id]["fail"] += 1
                one["error"] = str(e)
                log(f"  FAIL {time.time()-t0:.1f}s {e}")
            results.append(one)
            if i % 5 == 0:
                result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            time.sleep(0.4)

    summary = {
        "han_assigned": len(han_rows),
        "zhao_assigned": len(zhao_rows),
        "han": stats[HAN_ID],
        "zhao": stats[ZHAO_ID],
        "quota_han": quota.count(HAN_ID),
        "quota_zhao": quota.count(ZHAO_ID),
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / f"split_publish_han300_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
