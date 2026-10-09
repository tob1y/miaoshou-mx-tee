# -*- coding: utf-8 -*-
"""小赵店：认领串行 + 改品/识图并发上架。

限流：客户端内置 QPS 退避；认领单独串行锁。
改品：ThreadPoolExecutor，skip_reclaim（刚认领过不再抢 reclaim）。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one, qps_retry
from services.daily_quota import DailyQuota
from services.dual_end_pool import load_shared, mark_shared
from services.transformer import transform_product

ZHAO_ID = 18545044
ZHAO_NAME = "小赵1店"
TZ = timezone(timedelta(hours=8))
WORKERS = 4
CLAIM_BATCH = 8  # 每批评领多少再并发改品
# 小赵：今日池从旧→新
POOL_NEWEST_FIRST = False
WHO = "zhao"


def parse_gmt(s: str) -> datetime | None:
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def adapt_public_product(prod: dict) -> dict:
    out = dict(prod)
    color_name = str(out.get("colorPropName") or "Color")
    size_name = str(out.get("sizePropName") or "Tamaño")
    color_vals = []
    for key, val in (out.get("colorMap") or {}).items():
        name = val.get("name") if isinstance(val, dict) else key
        color_vals.append({"attrValue": str(name or key), "attrValueId": str(key)})
    size_vals = []
    for key, val in (out.get("sizeMap") or {}).items():
        name = val.get("name") if isinstance(val, dict) else key
        size_vals.append({"attrValue": str(name or key), "attrValueId": str(key)})
    props = []
    if color_vals:
        props.append({"name": color_name, "attrValueList": color_vals})
    if size_vals:
        props.append({"name": size_name, "attrValueList": size_vals})
    out["skuPropertyList"] = props
    return out


def already_done_sids() -> set[str]:
    """跳过：门禁留库 + 小赵已处理 + 小韩已上架（同批采集不交叉上架）。

    并发日志里 OK 行只有 detail=、没有 sid=，需用「预检 sid → 认领 detail」映射。
    """
    done: set[str] = set()

    def _ingest_json(glob_pat: str, *, ok_only: bool = False) -> None:
        for jp in (ROOT / "data/previews").glob(glob_pat):
            if jp.name.endswith("_state.json") or "summary" in jp.name:
                continue
            try:
                data = json.loads(jp.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for one in data:
                if not isinstance(one, dict):
                    continue
                sid = str(one.get("source_id") or "")
                if not sid:
                    continue
                if one.get("ok"):
                    done.add(sid)
                elif not ok_only and one.get("hold_without_publish"):
                    done.add(sid)

    # 小韩已上架：绝不给小赵再认领
    _ingest_json("han_concurrent_*.json", ok_only=True)
    # 小赵本店已处理（上架或留库）
    _ingest_json("zhao_concurrent_*.json", ok_only=False)

    for log in (ROOT / "data/previews").glob("zhao_concurrent_*.log"):
        last_sid = ""
        detail_sid: dict[str, str] = {}
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"sid=(\d+)", line)
            if m:
                last_sid = m.group(1)
            m = re.search(r"(?:已认领 detail|detailId)=(\d+)", line)
            if m and last_sid:
                detail_sid[m.group(1)] = last_sid
            if "OK 已上架" in line:
                md = re.search(r"detail=(\d+)", line)
                if md and md.group(1) in detail_sid:
                    done.add(detail_sid[md.group(1)])
                elif last_sid:
                    done.add(last_sid)
            if any(
                k in line
                for k in (
                    "文字留库",
                    "识图/改品留库",
                    "留库不上架",
                )
            ):
                md = re.search(r"detail=(\d+)", line)
                if md and md.group(1) in detail_sid:
                    done.add(detail_sid[md.group(1)])
                elif last_sid:
                    done.add(last_sid)

    # 小韩日志：仅 OK 上架进跳过（留库仍可给小赵用新规则试）
    for log in (ROOT / "data/previews").glob("han_concurrent_*.log"):
        last_sid = ""
        detail_sid: dict[str, str] = {}
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"sid=(\d+)", line)
            if m:
                last_sid = m.group(1)
            m = re.search(r"(?:已认领 detail|detailId)=(\d+)", line)
            if m and last_sid:
                detail_sid[m.group(1)] = last_sid
            if "OK 已上架" in line:
                md = re.search(r"detail=(\d+)", line)
                if md and md.group(1) in detail_sid:
                    done.add(detail_sid[md.group(1)])
                elif last_sid:
                    done.add(last_sid)

    for pat in ("chrono_publish_*.log", "split_publish_han100_zhao100_*.log"):
        for log in (ROOT / "data/previews").glob(pat):
            sid = ""
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.search(r"sid=(\d+)", line)
                if m:
                    sid = m.group(1)
                if not sid:
                    continue
                if any(
                    k in line
                    for k in (
                        "文字留库",
                        "识图/改品留库",
                        "留库不上架",
                    )
                ):
                    done.add(sid)
                    sid = ""
    return done


def list_success_since(client: MiaoshouClient, since: datetime) -> list[dict]:
    """公共箱 status=success 且 gmtCreate >= since。"""
    rows: dict[int, dict] = {}
    for page in range(1, 100):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "公共箱",
            ),
            "public_list",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            if str(it.get("status") or "").lower() != "success":
                continue
            gmt = parse_gmt(str(it.get("gmtCreate") or ""))
            if not gmt or gmt < since:
                continue
            cid = it.get("commonCollectBoxDetailId")
            sid = str(it.get("itemNum") or "")
            if not sid:
                for s in it.get("sourceList") or []:
                    if isinstance(s, dict) and s.get("sourceItemId"):
                        sid = str(s["sourceItemId"])
                        break
            if not sid or cid is None:
                continue
            rows[int(cid)] = {
                "common_id": int(cid),
                "sid": sid,
                "title": str(it.get("title") or "")[:160],
                "gmt": str(it.get("gmtCreate") or ""),
            }
        oldest = parse_gmt(str(batch[-1].get("gmtCreate") or ""))
        if oldest and oldest < since:
            break
        if len(batch) < 100:
            break
        time.sleep(0.15)
    return sorted(
        rows.values(),
        key=lambda x: x.get("gmt") or "",
        reverse=POOL_NEWEST_FIRST,
    )


def list_today_success(client: MiaoshouClient, today0: datetime) -> list[dict]:
    return list_success_since(client, today0)


def gate_text_only(client, row, scheme, blank, fill_urls) -> tuple[bool, str, str]:
    body = qps_retry(
        lambda cid=row["common_id"]: client.post(
            ep.PUBLIC_DETAIL, {"commonCollectBoxDetailId": int(cid)}
        ),
        "detail",
    )
    inner = (body.get("body") or body).get("data") or {}
    if "result" in (body.get("body") or {}) and (body.get("body") or {}).get("result") != "success":
        return False, str((body.get("body") or {}).get("message") or "detail_fail")[:120], ""
    prod = inner.get("editCommonCollectBoxDetail") or {}
    if not prod:
        return False, "no_detail", ""
    title = str(prod.get("oriTitle") or prod.get("title") or row.get("title") or "")
    report = transform_product(
        adapt_public_product(prod),
        scheme,
        blank,
        fill_image_urls=fill_urls,
        vision_cfg={"enabled": False},
    )
    hold_r = report.get("hold_without_publish") or {}
    if hold_r.get("hold"):
        return False, str(hold_r.get("reason") or "hold"), title
    return True, "ok", title


def main() -> int:
    workers = WORKERS
    for a in sys.argv[1:]:
        if a.isdigit():
            workers = max(1, min(10, int(a)))

    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"zhao_concurrent_{stamp}.log"
    result_path = out_dir / f"zhao_concurrent_{stamp}.json"
    state_path = out_dir / f"zhao_concurrent_{stamp}_state.json"
    log_lock = threading.Lock()

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_lock:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or ""),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=1.0,  # 并发下全局节流略加大，少撞 QPS
        max_retries=5,
    )
    scheme = json.loads((ROOT / "config/schemes/default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config/publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets/blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    vision_cfg = dict(settings.get("vision") or {})
    quota = DailyQuota(
        ROOT / "data/daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )

    done = already_done_sids()
    handled: set[str] = set()
    results: list[dict] = []
    stats = {"ok": 0, "held": 0, "fail": 0}
    claim_lock = threading.Lock()
    results_lock = threading.Lock()

    log(
        f"启动小赵并发：workers={workers} 配额剩={quota.remaining(ZHAO_ID)} "
        f"跳过种子={len(done)} 模型={vision_cfg.get('model')}"
    )

    def save_state() -> None:
        with results_lock:
            state_path.write_text(
                json.dumps(
                    {
                        "stats": stats,
                        "quota_zhao": quota.count(ZHAO_ID),
                        "handled": len(handled),
                        "results_n": len(results),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            result_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )

    def process_claimed(row: dict, detail_id: int, title: str) -> dict:
        sid = str(row["sid"])
        one: dict = {
            "shop_id": ZHAO_ID,
            "shop_name": ZHAO_NAME,
            "source_id": sid,
            "common_id": row["common_id"],
            "detail_id": detail_id,
            "title": title,
            "gmt": row.get("gmt"),
            "ok": False,
        }
        t0 = time.time()
        try:
            ep_res = edit_and_publish_one(
                client,
                detail_id=detail_id,
                shop_id=ZHAO_ID,
                shop_name=ZHAO_NAME,
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
            vision_m = hold.get("vision") if isinstance(hold, dict) else {}
            if isinstance(vision_m, dict) and vision_m.get("images_scanned") is not None:
                one["vision_metrics"] = {
                    "source_id": sid,
                    "detail_id": detail_id,
                    "hold": bool(hold.get("hold")),
                    "reason": hold.get("reason") or vision_m.get("reason"),
                    "images_scanned": vision_m.get("images_scanned"),
                    "elapsed_s": vision_m.get("elapsed_s"),
                    "cny": vision_m.get("cny"),
                    "cache_hits": vision_m.get("cache_hits"),
                }
            if one.get("ok"):
                with results_lock:
                    stats["ok"] += 1
                    quota.add(ZHAO_ID, 1)
                log(
                    f"  OK 已上架 {time.time()-t0:.1f}s detail={detail_id} "
                    f"({stats['ok']}) 配额={quota.count(ZHAO_ID)}"
                )
            elif one.get("held_in_box") or hold.get("hold"):
                with results_lock:
                    stats["held"] += 1
                log(f"  识图/改品留库 {hold.get('reason')}")
            else:
                with results_lock:
                    stats["fail"] += 1
                log(f"  FAIL publish detail={detail_id}")
        except Exception as e:
            with results_lock:
                stats["fail"] += 1
            one["error"] = str(e)[:300]
            log(f"  FAIL {e}")
        with results_lock:
            results.append(one)
            handled.add(sid)
        return one

    idle = 0
    cached_pool: list[dict] = []
    today_s = today0.strftime("%Y-%m-%d")
    direction = "新→旧" if POOL_NEWEST_FIRST else "旧→新"
    log(f"双端模式：今日池 {direction}；满配额或与对店撞上则结束")

    def refresh_skip() -> None:
        done.update(load_shared(ROOT, today_s))

    def note_sid(sid: str) -> None:
        handled.add(sid)
        mark_shared(ROOT, today_s, sid, WHO)

    while quota.remaining(ZHAO_ID) > 0:
        refresh_skip()
        pending = [r for r in cached_pool if r["sid"] not in done and r["sid"] not in handled]
        if len(pending) < CLAIM_BATCH * 2:
            cached_pool = list_success_since(client, today0)
            refresh_skip()
            pending = [r for r in cached_pool if r["sid"] not in done and r["sid"] not in handled]
        log(
            f"扫描 today={len(cached_pool)}/{len(pending)} dir={direction} "
            f"ok={stats['ok']} held={stats['held']} fail={stats['fail']} "
            f"配额剩={quota.remaining(ZHAO_ID)}"
        )
        if not pending:
            idle += 1
            if idle >= 2:
                log("今日池已啃完（两头撞上或无货），结束")
                break
            log(f"暂无 pending，20s 后再确认是否撞上 ({idle}/2)")
            cached_pool = []
            time.sleep(20)
            continue
        idle = 0

        # 取一批做文字预检
        batch_need = min(CLAIM_BATCH, quota.remaining(ZHAO_ID), len(pending))
        claimed: list[tuple[dict, int, str]] = []
        i = 0
        while len(claimed) < batch_need and i < len(pending) and quota.remaining(ZHAO_ID) > 0:
            row = pending[i]
            i += 1
            sid = str(row["sid"])
            refresh_skip()
            if sid in handled or sid in done:
                continue
            note_sid(sid)
            log(f"[预检] gmt={row.get('gmt')} common={row['common_id']} sid={sid}")
            try:
                ok_t, reason_t, title = gate_text_only(client, row, scheme, blank, fill_urls)
            except Exception as e:
                log(f"  预检异常 {e}")
                with results_lock:
                    stats["fail"] += 1
                    results.append(
                        {
                            "source_id": sid,
                            "common_id": row["common_id"],
                            "ok": False,
                            "error": str(e)[:200],
                        }
                    )
                continue
            if not ok_t:
                with results_lock:
                    stats["held"] += 1
                    results.append(
                        {
                            "source_id": sid,
                            "common_id": row["common_id"],
                            "title": title,
                            "ok": False,
                            "held_in_box": True,
                            "hold_without_publish": {"hold": True, "reason": reason_t, "title": title[:140]},
                        }
                    )
                log(f"  文字留库 {reason_t}")
                continue

            # 认领串行
            with claim_lock:
                try:
                    detail_id = claim_to_detail(
                        client,
                        {
                            "commonCollectBoxDetailId": row["common_id"],
                            "itemNum": sid,
                            "status": "success",
                        },
                        ZHAO_ID,
                        sid,
                        log,
                    )
                    qps_retry(
                        lambda d=detail_id: client.claim_to_shops([d], [ZHAO_ID]),
                        "claim_shop",
                        log=log,
                    )
                    time.sleep(0.35)
                except Exception as e:
                    log(f"  认领失败 {e}")
                    with results_lock:
                        stats["fail"] += 1
                    continue
            claimed.append((row, int(detail_id), title or row.get("title") or ""))
            log(f"  已认领 detail={detail_id} → 并发队列 ({len(claimed)}/{batch_need})")

        if not claimed:
            continue

        log(f"=== 并发改品上架 {len(claimed)} 条 workers={workers} ===")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [
                ex.submit(process_claimed, row, did, title) for row, did, title in claimed
            ]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    log(f"  worker 异常 {e}")
        save_state()
        time.sleep(0.5)

    save_state()
    summary = {
        "stamp": stamp,
        "zhao": stats,
        "quota_zhao": quota.count(ZHAO_ID),
        "workers": workers,
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"zhao_concurrent_{stamp}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
