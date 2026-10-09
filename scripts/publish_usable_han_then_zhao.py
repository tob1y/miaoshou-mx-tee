# -*- coding: utf-8 -*-
"""今日识图可用队列：先小韩上 TARGET 条，再小赵上 TARGET 条。"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one, qps_retry
from services.daily_quota import DailyQuota
from services.dual_end_pool import load_shared, mark_shared

HAN_ID, HAN_NAME = 18545217, "小韩1店"
ZHAO_ID, ZHAO_NAME = 18545044, "小赵1店"
TZ = timezone(timedelta(hours=8))
WORKERS = 4
CLAIM_BATCH = 8
TARGET = 150
QUEUE = ROOT / "data/previews/usable_publish_queue_20261008.json"


def main() -> int:
    han_target = TARGET
    zhao_target = TARGET
    order = "han,zhao"  # or zhao,han
    queue_path = QUEUE
    for a in sys.argv[1:]:
        if a.startswith("--target="):
            n = max(1, int(a.split("=", 1)[1]))
            han_target = zhao_target = n
        elif a.startswith("--han-target="):
            han_target = max(0, int(a.split("=", 1)[1]))
        elif a.startswith("--zhao-target="):
            zhao_target = max(0, int(a.split("=", 1)[1]))
        elif a.startswith("--order="):
            order = a.split("=", 1)[1].strip().lower()
        elif a.startswith("--queue="):
            queue_path = Path(a.split("=", 1)[1])
        elif a.isdigit():
            han_target = zhao_target = max(1, int(a))

    today_s = datetime.now(TZ).strftime("%Y-%m-%d")
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"publish_usable_han_zhao_{stamp}.log"
    result_path = out_dir / f"publish_usable_han_zhao_{stamp}.json"
    log_lock = threading.Lock()

    def log(msg: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        with log_lock:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    queue = list(payload.get("rows") or [])
    # 可用队列已识图筛过：默认上架不再二次识图（可用 --vision 打开）
    vision_on = "--vision" in sys.argv
    vision_cfg = dict(settings.get("vision") or {})
    vision_cfg["enabled"] = bool(vision_on)
    log(
        f"可用队列 {len(queue)} 条 ← {queue_path.name}；"
        f"目标 韩={han_target} 赵={zhao_target} order={order}；"
        f"二次识图={'开' if vision_on else '关（先上架）'}"
    )

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or ""),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=1.0,
        max_retries=5,
    )
    scheme = json.loads((ROOT / "config/schemes/default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config/publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets/blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    quota = DailyQuota(
        ROOT / "data/daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )

    results: list[dict] = []
    results_lock = threading.Lock()
    claim_lock = threading.Lock()
    used: set[str] = set(load_shared(ROOT, today_s))

    def publish_shop(shop_id: int, shop_name: str, who: str, need: int) -> dict:
        stats = {"ok": 0, "held": 0, "fail": 0}
        already = quota.count(shop_id)
        remain_quota = quota.remaining(shop_id)
        # 续跑：已上过的计入目标，只补差额
        need = min(max(0, need - already), remain_quota)
        if need <= 0:
            log(f"=== 跳过 {shop_name} 已达目标或无配额 already={already} 剩={remain_quota} ===")
            return stats
        log(
            f"=== 开始 {shop_name} 本轮补={need} 已有={already} "
            f"目标合计≈{already + need} 配额剩={remain_quota} ==="
        )

        pending = [
            r
            for r in queue
            if str(r.get("sid") or "") not in used
        ]
        i = 0

        def process_claimed(row: dict, detail_id: int) -> None:
            sid = str(row["sid"])
            one: dict = {
                "shop_id": shop_id,
                "shop_name": shop_name,
                "source_id": sid,
                "common_id": row["common_id"],
                "detail_id": detail_id,
                "title": row.get("title"),
                "gmt": row.get("gmt"),
                "ok": False,
            }
            t0 = time.time()
            try:
                ep_res = edit_and_publish_one(
                    client,
                    detail_id=detail_id,
                    shop_id=shop_id,
                    shop_name=shop_name,
                    scheme=scheme,
                    publish_cfg=publish_cfg,
                    blank_dir=blank,
                    fill_urls=fill_urls,
                    log=log,
                    vision_cfg=vision_cfg,
                    skip_reclaim=True,
                )
                one.update({k: v for k, v in ep_res.items() if k != "prep"})
                hold = one.get("hold_without_publish") or {}
                if one.get("ok"):
                    with results_lock:
                        stats["ok"] += 1
                        quota.add(shop_id, 1)
                    log(
                        f"  OK {shop_name} {time.time()-t0:.1f}s detail={detail_id} "
                        f"({stats['ok']}/{need}) 配额={quota.count(shop_id)}"
                    )
                elif one.get("held_in_box") or (isinstance(hold, dict) and hold.get("hold")):
                    with results_lock:
                        stats["held"] += 1
                    log(f"  留库 {shop_name} {hold.get('reason') if isinstance(hold, dict) else ''}")
                else:
                    with results_lock:
                        stats["fail"] += 1
                    log(f"  FAIL {shop_name} detail={detail_id}")
            except Exception as e:
                with results_lock:
                    stats["fail"] += 1
                one["error"] = str(e)[:300]
                log(f"  FAIL {shop_name} {e}")
            with results_lock:
                results.append(one)

        while stats["ok"] < need and i < len(pending) and quota.remaining(shop_id) > 0:
            batch_need = min(CLAIM_BATCH, need - stats["ok"], quota.remaining(shop_id))
            claimed: list[tuple[dict, int]] = []
            while len(claimed) < batch_need and i < len(pending) and quota.remaining(shop_id) > 0:
                row = pending[i]
                i += 1
                sid = str(row["sid"])
                used.update(load_shared(ROOT, today_s))
                if sid in used:
                    continue
                used.add(sid)
                mark_shared(ROOT, today_s, sid, who)
                log(f"[{shop_name}] common={row['common_id']} sid={sid}")
                with claim_lock:
                    try:
                        detail_id = claim_to_detail(
                            client,
                            {
                                "commonCollectBoxDetailId": int(row["common_id"]),
                                "itemNum": sid,
                                "status": "success",
                            },
                            shop_id,
                            sid,
                            log,
                        )
                        qps_retry(
                            lambda d=detail_id, s=shop_id: client.claim_to_shops([d], [s]),
                            "claim_shop",
                            log=log,
                        )
                        time.sleep(0.35)
                    except Exception as e:
                        log(f"  认领失败 {e}")
                        with results_lock:
                            stats["fail"] += 1
                            results.append(
                                {
                                    "shop_id": shop_id,
                                    "shop_name": shop_name,
                                    "source_id": sid,
                                    "common_id": row["common_id"],
                                    "ok": False,
                                    "error": str(e)[:200],
                                }
                            )
                        continue
                claimed.append((row, int(detail_id)))
                log(f"  已认领 detail={detail_id} ({len(claimed)}/{batch_need})")

            if not claimed:
                if i >= len(pending):
                    break
                continue

            log(f"=== {shop_name} 并发上架 {len(claimed)} workers={WORKERS} ===")
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = [ex.submit(process_claimed, row, did) for row, did in claimed]
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception as e:
                        log(f"  worker 异常 {e}")
            result_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )

        log(f"=== {shop_name} 结束 ok={stats['ok']} held={stats['held']} fail={stats['fail']} ===")
        return stats

    shops = []
    if order.startswith("zhao"):
        shops = [
            (ZHAO_ID, ZHAO_NAME, "zhao", zhao_target),
            (HAN_ID, HAN_NAME, "han", han_target),
        ]
    else:
        shops = [
            (HAN_ID, HAN_NAME, "han", han_target),
            (ZHAO_ID, ZHAO_NAME, "zhao", zhao_target),
        ]
    shop_stats: dict[str, dict] = {}
    for sid, sname, who, tgt in shops:
        shop_stats[who] = publish_shop(sid, sname, who, tgt)
    han_stats = shop_stats.get("han") or {"ok": 0, "held": 0, "fail": 0}
    zhao_stats = shop_stats.get("zhao") or {"ok": 0, "held": 0, "fail": 0}

    summary = {
        "stamp": stamp,
        "han_target": han_target,
        "zhao_target": zhao_target,
        "order": order,
        "han": han_stats,
        "zhao": zhao_stats,
        "quota": {
            "han": quota.count(HAN_ID),
            "zhao": quota.count(ZHAO_ID),
        },
        "log": str(log_path),
        "json": str(result_path),
    }
    log(f"=== 全部结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"publish_usable_han_zhao_{stamp}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
