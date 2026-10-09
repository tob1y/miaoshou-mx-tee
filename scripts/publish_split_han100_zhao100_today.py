# -*- coding: utf-8 -*-
"""今天公共箱 success：过门禁后小韩/小赵各上 100 条。"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one, qps_retry
from services.daily_quota import DailyQuota
from services.transformer import transform_product

HAN_ID = 18545217
HAN_NAME = "小韩1店"
ZHAO_ID = 18545044
ZHAO_NAME = "小赵1店"
PER_SHOP = 100
TZ = timezone(timedelta(hours=8))


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


def already_published_sids() -> set[str]:
    """今天已经上架成功的货源，避免第二批重复发。"""
    done: set[str] = set()
    for log in (ROOT / "data/previews").glob("split_publish_han100_zhao100_*.log"):
        sid = ""
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"sid=(\d+)", line)
            if m and "common=" in line:
                sid = m.group(1)
                continue
            if sid and "OK 已上架" in line:
                done.add(sid)
                sid = ""
    return done


def list_today_success(client: MiaoshouClient, today0: datetime) -> list[dict]:
    rows: dict[int, dict] = {}
    for page in range(1, 40):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "public",
            ),
            f"list_{page}",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            dt = parse_gmt(str(it.get("gmtCreate") or ""))
            if not dt or dt < today0:
                continue
            if str(it.get("status") or "").lower() != "success":
                continue
            sid = str(it.get("itemNum") or it.get("sourceItemId") or "")
            cid = it.get("commonCollectBoxDetailId")
            if not sid or cid is None:
                continue
            rows[int(cid)] = {
                "common_id": int(cid),
                "sid": sid,
                "title": str(it.get("title") or "")[:120],
                "gmt": str(it.get("gmtCreate") or ""),
            }
        oldest = parse_gmt(str(batch[-1].get("gmtCreate") or ""))
        if oldest and oldest < today0:
            break
        if len(batch) < 100:
            break
        time.sleep(0.25)
    # newest first
    return sorted(rows.values(), key=lambda x: x.get("gmt") or "", reverse=True)


def gate_can_publish(
    client: MiaoshouClient,
    row: dict,
    scheme: dict,
    blank: Path,
    fill_urls: list[str],
    vision_cfg: dict | None = None,
) -> tuple[bool, str]:
    body = qps_retry(
        lambda cid=row["common_id"]: client.post(
            ep.PUBLIC_DETAIL, {"commonCollectBoxDetailId": int(cid)}
        ),
        "detail",
    )
    inner = (body.get("body") or body).get("data") or {}
    if "result" in (body.get("body") or {}) and (body.get("body") or {}).get("result") != "success":
        return False, str((body.get("body") or {}).get("message") or "detail_fail")[:120]
    prod = inner.get("editCommonCollectBoxDetail") or {}
    if not prod:
        return False, "no_detail"
    report = transform_product(
        adapt_public_product(prod),
        scheme,
        blank,
        fill_image_urls=fill_urls,
        vision_cfg=vision_cfg,
    )
    hold_r = report.get("hold_without_publish") or {}
    if hold_r.get("hold"):
        return False, str(hold_r.get("reason") or "hold")
    return True, "ok"


def publish_batch(
    *,
    client: MiaoshouClient,
    shop_id: int,
    shop_name: str,
    batch: list[dict],
    scheme: dict,
    publish_cfg: dict,
    blank: Path,
    fill_urls: list[str],
    quota: DailyQuota,
    log,
    stats: dict,
    results: list,
    result_path: Path,
    vision_cfg: dict | None = None,
) -> None:
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

            def _once() -> dict:
                t_claim = time.time()
                detail_id = claim_to_detail(
                    client,
                    {
                        "commonCollectBoxDetailId": common_id,
                        "itemNum": sid,
                        "status": "success",
                    },
                    shop_id,
                    sid,
                    log,
                )
                claim_s = round(time.time() - t_claim, 3)
                one["detail_id"] = detail_id
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
                    vision_cfg=vision_cfg,
                )
                timing = dict(ep.get("timing") or {})
                timing["claim_s"] = claim_s
                timing["wall_s"] = round(time.time() - t0, 3)
                ep["timing"] = timing
                return ep

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
            hold = ep_res.get("hold_without_publish") or {}
            timing = ep_res.get("timing") or {}
            if timing:
                one["timing"] = timing
                log(
                    f"  分段耗时 claim={timing.get('claim_s')}s reclaim={timing.get('reclaim_s')}s "
                    f"detail={timing.get('get_detail_s')}s vision={timing.get('vision_s')}s "
                    f"mutate={timing.get('text_and_mutate_s')}s save={timing.get('save_s')}s "
                    f"publish={timing.get('publish_api_s')}s total={timing.get('wall_s') or timing.get('total_s')}s"
                )
            vision_m = hold.get("vision") or {}
            if isinstance(vision_m, dict) and (
                vision_m.get("images_scanned") or vision_m.get("prompt_tokens")
            ):
                one["vision_metrics"] = {
                    "source_id": sid,
                    "common_id": common_id,
                    "detail_id": one.get("detail_id"),
                    "hold": bool(vision_m.get("hold")),
                    "reason": vision_m.get("reason"),
                    "images_scanned": vision_m.get("images_scanned"),
                    "images_total": vision_m.get("images_total"),
                    "elapsed_s": vision_m.get("elapsed_s"),
                    "prompt_tokens": vision_m.get("prompt_tokens"),
                    "completion_tokens": vision_m.get("completion_tokens"),
                    "total_tokens": vision_m.get("total_tokens"),
                    "cny": vision_m.get("cny"),
                    "usd": vision_m.get("usd"),
                    "image_detail": vision_m.get("image_detail"),
                    "hits": vision_m.get("hits"),
                    "timing": timing,
                }
                log(
                    f"  识图记录 图={vision_m.get('images_scanned')}/{vision_m.get('images_total')} "
                    f"{vision_m.get('elapsed_s')}s tok={vision_m.get('prompt_tokens')}+{vision_m.get('completion_tokens')} "
                    f"¥{vision_m.get('cny')} reason={vision_m.get('reason')}"
                )
            if ep_res.get("held_in_box"):
                stats[shop_id]["held"] += 1
                log(f"  留库 {time.time()-t0:.1f}s reason={hold.get('reason')}")
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
            result_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        time.sleep(0.4)


def main() -> int:
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    mode = ""
    target_override: int | None = None
    no_vision = False
    for a in args:
        low = a.lower()
        if low in {"zhao", "han", "fill", "fill-zhao"}:
            mode = low
        elif low in {"--no-vision", "no-vision", "text"}:
            no_vision = True
        elif a.isdigit():
            target_override = int(a)
    zhao_only = mode == "zhao"
    han_only = mode == "han"
    fill_zhao = mode in {"fill", "fill-zhao"}
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"split_publish_han100_zhao100_{stamp}.log"
    result_path = out_dir / f"split_publish_han100_zhao100_{stamp}.json"
    preview_path = out_dir / f"public_can_publish_han100_zhao100_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

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
    vision_cfg = dict(settings.get("vision") or {})
    if no_vision:
        vision_cfg["enabled"] = False
    # 预检只跑文字门禁，避免同一条识图两次烧钱；真正识图在上架改品时记录
    vision_precheck = dict(vision_cfg)
    vision_precheck["enabled"] = False
    quota = DailyQuota(
        ROOT / "data/daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )
    if fill_zhao:
        target_can = quota.remaining(ZHAO_ID) + quota.remaining(HAN_ID)
    elif zhao_only:
        target_can = quota.remaining(ZHAO_ID)
    elif han_only:
        target_can = min(target_override or PER_SHOP, quota.remaining(HAN_ID))
    else:
        target_can = PER_SHOP * 2

    log(
        f"扫描今天 success；配额剩 韩={quota.remaining(HAN_ID)} 赵={quota.remaining(ZHAO_ID)}；"
        f"本批目标 {target_can}；识图={'关' if no_vision else '开'}"
    )
    pool = list_today_success(client, today0)
    # sid 去重保序（新→旧）
    uniq: list[dict] = []
    seen: set[str] = set()
    for r in pool:
        sid = str(r.get("sid") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        uniq.append(r)
    done = already_published_sids()
    before = len(uniq)
    uniq = [r for r in uniq if str(r.get("sid") or "") not in done]
    log(f"今天 success 去重 {before}，已上架跳过 {before - len(uniq)}，本批 {len(uniq)}")

    can_rows: list[dict] = []
    hold_n = err_n = 0
    for i, row in enumerate(uniq, 1):
        one = dict(row)
        try:
            ok, reason = gate_can_publish(client, row, scheme, blank, fill_urls, vision_precheck)
            one["can_publish"] = ok
            one["reason"] = reason
            if ok:
                can_rows.append(one)
            else:
                hold_n += 1
        except Exception as e:
            err_n += 1
            one["can_publish"] = False
            one["reason"] = str(e)[:120]
            hold_n += 1
        if i % 25 == 0 or i == len(uniq):
            log(f"  预检 [{i}/{len(uniq)}] 可上架={len(can_rows)} 留库/错={hold_n}")
            preview_path.write_text(
                json.dumps({"can": can_rows, "scanned": i, "hold": hold_n, "err": err_n}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if len(can_rows) >= target_can:
            log(f"  已够 {target_can} 条可上架，停止预检")
            break
        time.sleep(0.12)

    if fill_zhao:
        zhao_n = min(len(can_rows), quota.remaining(ZHAO_ID))
        zhao_rows = can_rows[:zhao_n]
        han_rows = can_rows[zhao_n : zhao_n + quota.remaining(HAN_ID)]
    elif zhao_only:
        han_rows = []
        zhao_rows = can_rows[: quota.remaining(ZHAO_ID)]
    elif han_only:
        zhao_rows = []
        han_rows = can_rows[: min(target_can, quota.remaining(HAN_ID))]
    else:
        han_rows = can_rows[:PER_SHOP]
        zhao_rows = can_rows[PER_SHOP : PER_SHOP * 2]
    log(
        f"分配：{HAN_NAME} {len(han_rows)}（配额剩 {quota.remaining(HAN_ID)}）；"
        f"{ZHAO_NAME} {len(zhao_rows)}（配额剩 {quota.remaining(ZHAO_ID)}）；可上架池 {len(can_rows)}"
    )

    results: list[dict] = []
    stats = {HAN_ID: {"ok": 0, "held": 0, "fail": 0}, ZHAO_ID: {"ok": 0, "held": 0, "fail": 0}}
    shops = (
        [(ZHAO_ID, ZHAO_NAME, zhao_rows), (HAN_ID, HAN_NAME, han_rows)]
        if fill_zhao
        else [(HAN_ID, HAN_NAME, han_rows), (ZHAO_ID, ZHAO_NAME, zhao_rows)]
    )
    publish_batch(
        client=client,
        shop_id=shops[0][0],
        shop_name=shops[0][1],
        batch=shops[0][2],
        scheme=scheme,
        publish_cfg=publish_cfg,
        blank=blank,
        fill_urls=fill_urls,
        quota=quota,
        log=log,
        stats=stats,
        results=results,
        result_path=result_path,
        vision_cfg=vision_cfg,
    )
    publish_batch(
        client=client,
        shop_id=shops[1][0],
        shop_name=shops[1][1],
        batch=shops[1][2],
        scheme=scheme,
        publish_cfg=publish_cfg,
        blank=blank,
        fill_urls=fill_urls,
        quota=quota,
        log=log,
        stats=stats,
        results=results,
        result_path=result_path,
        vision_cfg=vision_cfg,
    )

    summary = {
        "can_pool": len(can_rows),
        "han_assigned": len(han_rows),
        "zhao_assigned": len(zhao_rows),
        "han": stats[HAN_ID],
        "zhao": stats[ZHAO_ID],
        "quota_han": quota.count(HAN_ID),
        "quota_zhao": quota.count(ZHAO_ID),
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # 识图计量汇总
    vrows = [r.get("vision_metrics") for r in results if isinstance(r.get("vision_metrics"), dict)]
    if vrows:
        metrics = {
            "stamp": stamp,
            "model": vision_cfg.get("model"),
            "image_detail": vision_cfg.get("image_detail"),
            "n": len(vrows),
            "hold_n": sum(1 for v in vrows if v.get("hold")),
            "pass_n": sum(1 for v in vrows if not v.get("hold")),
            "sum_images_scanned": sum(int(v.get("images_scanned") or 0) for v in vrows),
            "sum_elapsed_s": round(sum(float(v.get("elapsed_s") or 0) for v in vrows), 2),
            "sum_prompt_tokens": sum(int(v.get("prompt_tokens") or 0) for v in vrows),
            "sum_completion_tokens": sum(int(v.get("completion_tokens") or 0) for v in vrows),
            "sum_cny": round(sum(float(v.get("cny") or 0) for v in vrows), 4),
            "avg_s_per_link": round(
                sum(float(v.get("elapsed_s") or 0) for v in vrows) / max(1, len(vrows)), 2
            ),
            "avg_images_per_link": round(
                sum(int(v.get("images_scanned") or 0) for v in vrows) / max(1, len(vrows)), 2
            ),
            "avg_tokens_per_link": round(
                sum(int(v.get("total_tokens") or 0) for v in vrows) / max(1, len(vrows)), 1
            ),
            "avg_cny_per_link": round(
                sum(float(v.get("cny") or 0) for v in vrows) / max(1, len(vrows)), 5
            ),
            "avg_s_per_image": round(
                (
                    sum(float(v.get("elapsed_s") or 0) for v in vrows)
                    / max(1, sum(int(v.get("images_scanned") or 0) for v in vrows))
                ),
                2,
            )
            if sum(int(v.get("images_scanned") or 0) for v in vrows)
            else None,
            "by_reason": {},
            "items": vrows,
        }
        reasons: dict[str, int] = {}
        for r in results:
            hold = r.get("hold_without_publish") or {}
            why = str(hold.get("reason") or ("ok" if r.get("ok") else "fail"))
            reasons[why] = reasons.get(why, 0) + 1
        metrics["by_reason"] = reasons
        metrics_path = out_dir / f"vision_metrics_{stamp}.json"
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        log(
            f"识图汇总 条={metrics['n']} 图={metrics['sum_images_scanned']} "
            f"均时/条={metrics['avg_s_per_link']}s 均时/图={metrics['avg_s_per_image']}s "
            f"均token/条={metrics['avg_tokens_per_link']} 费用≈¥{metrics['sum_cny']} → {metrics_path.name}"
        )
    (out_dir / f"split_publish_han100_zhao100_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
