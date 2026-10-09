# -*- coding: utf-8 -*-
"""1) 飞书未尝试链接批量采集  2) 合并失败表里已 success 的  3) 一起改品上架小韩。"""
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

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.auto_batch import (
    claim_to_detail,
    edit_and_publish_one,
    fresh_pending_rows,
    list_public_by_source,
    pick_success_public,
)
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable
from services.link_resolve import resolve_tiktok_link

SHOP_ID = 18545217
SHOP_NAME = "小韩1店"
TARGET_OK = 100


def main() -> int:
    target = TARGET_OK
    if len(sys.argv) > 1:
        target = int(sys.argv[1])

    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = dict(settings.get("feishu") or {})
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"han_collect_then_publish_{stamp}.log"
    plan_path = out_dir / f"han_collect_plan_{stamp}.json"
    result_path = out_dir / f"han_collect_then_publish_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    feishu = FeishuBitable(
        str(fcfg["app_id"]),
        str(fcfg["app_secret"]),
        str(fcfg["bitable_app_token"]),
        str(fcfg["bitable_table_id"]),
    )
    fail_token = str(fcfg.get("fail_collect_app_token") or "").strip()
    fail_table = str(fcfg.get("fail_collect_table_id") or "").strip()
    fail_box = None
    if fail_token and fail_table:
        fail_box = FeishuBitable(
            str(fcfg["app_id"]),
            str(fcfg["app_secret"]),
            fail_token,
            fail_table,
        )
        fail_box._token = feishu._token
        fail_box._token_expire_at = feishu._token_expire_at

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
    remain = quota.remaining(SHOP_ID)
    if remain <= 0:
        log("小韩今日配额已满")
        return 1
    target = min(target, remain)

    yes = fcfg.get("yes_value") or "是"
    no = fcfg.get("no_value") or "否"
    inbound_f = fcfg.get("inbound_status_field") or "入库状态"
    reason_f = fcfg.get("hold_reason_field") or "门禁原因"
    shop_f = fcfg.get("shop_field") or "分配店铺"

    # ---------- Phase 1: Feishu 未尝试 → 批量采集 ----------
    records = feishu.list_records(page_size=500)
    pending = fresh_pending_rows(records, fcfg)
    log(f"Phase1 飞书未尝试 {len(pending)} 条，开始批量采集…")

    feishu_ok: list[dict] = []
    feishu_fail = 0
    for i, row in enumerate(pending, 1):
        link = row["link"]
        rid = row["record_id"]
        log(f"[collect {i}/{len(pending)}] {link[:70]}")
        try:
            source_id, collect_url = resolve_tiktok_link(link)
            if not source_id:
                raise MiaoshouError(f"无法解析商品ID: {link}")
            items = list_public_by_source(client, source_id)
            hit = pick_success_public(items, source_id)
            if not hit:
                log(f"  fetch {collect_url}")
                try:
                    client.fetch_item([collect_url])
                except Exception as e:
                    log(f"  fetch warn: {e}")
                for attempt in range(1, 9):
                    items = list_public_by_source(client, source_id)
                    hit = pick_success_public(items, source_id)
                    if hit:
                        break
                    if items and attempt >= 3:
                        statuses = sorted({str(it.get("status") or "") for it in items})
                        if "success" not in {s.lower() for s in statuses}:
                            log(f"  提前结束 statuses={statuses}")
                            break
                    time.sleep(2)
            if not hit:
                raise MiaoshouError(f"公共箱无 success: {source_id}")
            common_id = int(hit["commonCollectBoxDetailId"])
            feishu_ok.append(
                {
                    "source": "feishu_pending",
                    "sid": str(source_id),
                    "common_id": common_id,
                    "link": link,
                    "record_id": rid,
                    "title": str(hit.get("title") or "")[:80],
                }
            )
            log(f"  OK collect common={common_id}")
            # 采集成功先不写门禁原因，留给上架阶段回写；避免中断后被当成已尝试跳过
        except Exception as e:
            feishu_fail += 1
            err = str(e)
            if len(err) > 200:
                err = err[:200] + "…"
            log(f"  FAIL collect {err[:120]}")
            try:
                feishu.update_record(
                    rid,
                    {
                        inbound_f: no,
                        reason_f: err,
                        shop_f: SHOP_NAME,
                    },
                )
            except Exception as e2:
                log(f"  飞书回写失败: {e2}")
        time.sleep(0.5)

    log(f"Phase1 结束: collect_ok={len(feishu_ok)} collect_fail={feishu_fail}")

    # ---------- Phase 2: 失败表里现已 success ----------
    fail_ok: list[dict] = []
    if fail_box is None:
        log("Phase2 跳过：未配置失败表")
    else:
        fail_recs = fail_box.list_records(page_size=500)
        link_ff = fcfg.get("fail_collect_link_field") or "上传链接"
        source_ff = fcfg.get("fail_collect_source_field") or "货源ID"
        reason_ff = fcfg.get("fail_collect_reason_field") or "失败原因"
        time_ff = fcfg.get("fail_collect_time_field") or "失败时间"
        from services.link_resolve import extract_url_field

        seen_sid: set[str] = {x["sid"] for x in feishu_ok}
        candidates = []
        for it in fail_recs:
            fields = it.get("fields") or {}
            link = extract_url_field(fields.get(link_ff)) or ""
            sid = str(fields.get(source_ff) or "").strip()
            if not sid:
                mm = re.search(r"(1\d{15,})", str(fields.get(reason_ff) or ""))
                if mm:
                    sid = mm.group(1)
            tval = fields.get(time_ff)
            try:
                ts = int(tval) if tval is not None else int(it.get("created_time") or 0)
            except Exception:
                ts = int(it.get("created_time") or 0)
            if sid:
                candidates.append(
                    {
                        "sid": sid,
                        "link": link,
                        "fail_record_id": it.get("record_id"),
                        "ts": ts,
                    }
                )

        # 优先最近失败（用户刚手动补采的），最多查 150 个货源
        candidates.sort(key=lambda x: int(x.get("ts") or 0), reverse=True)
        uniq_c: dict[str, dict] = {}
        for c in candidates:
            if c["sid"] in seen_sid or c["sid"] in uniq_c:
                continue
            uniq_c[c["sid"]] = c
            if len(uniq_c) >= 150:
                break
        log(f"Phase2 失败表候选(最近) unique_sid={len(uniq_c)}")

        for i, (sid, meta) in enumerate(uniq_c.items(), 1):
            try:
                body = client.assert_success(
                    client.post(
                        ep.PUBLIC_LIST,
                        {"pageNo": 1, "pageSize": 20, "filter": {"sourceItemIdKeyword": sid}},
                    ),
                    "pub",
                )
                batch = (body.get("data") or {}).get("detailList") or []
                successes = [
                    it
                    for it in batch
                    if str(it.get("status")) == "success"
                    and (
                        str(it.get("itemNum") or "") == sid
                        or str(it.get("sourceItemId") or "") == sid
                        or any(
                            str((s or {}).get("sourceItemId") or "") == sid
                            for s in (it.get("sourceList") or [])
                            if isinstance(s, dict)
                        )
                    )
                ]
                if not successes:
                    continue
                it = successes[0]
                fail_ok.append(
                    {
                        "source": "fail_table",
                        "sid": sid,
                        "common_id": int(it["commonCollectBoxDetailId"]),
                        "link": meta.get("link"),
                        "title": str(it.get("title") or "")[:80],
                        "fail_record_id": meta.get("fail_record_id"),
                    }
                )
                seen_sid.add(sid)
                log(f"[fail_ok {len(fail_ok)}] {sid} common={it.get('commonCollectBoxDetailId')}")
            except Exception as e:
                log(f"  fail_lookup err {sid}: {e}")
            if i % 10 == 0:
                time.sleep(0.3)
            else:
                time.sleep(0.8)

    plan = {
        "feishu_collect_ok": feishu_ok,
        "fail_table_ok": fail_ok,
        "feishu_collect_fail": feishu_fail,
    }
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    pool = feishu_ok + fail_ok
    # 再按 sid 去重，feishu 优先
    uniq_pool: dict[str, dict] = {}
    for r in pool:
        sid = str(r.get("sid") or "")
        if sid and sid not in uniq_pool:
            uniq_pool[sid] = r
    targets = list(uniq_pool.values())
    log(
        f"合并待上架: feishu_ok={len(feishu_ok)} fail_ok={len(fail_ok)} "
        f"去重后={len(targets)} → {SHOP_NAME} 目标成功 {target}"
    )

    # ---------- Phase 3: 上架小韩 ----------
    results = []
    n_ok = n_hold = n_fail = 0
    for i, row in enumerate(targets, 1):
        if n_ok >= target:
            break
        if quota.remaining(SHOP_ID) <= 0:
            log("配额用尽")
            break
        sid = str(row["sid"])
        common_id = int(row["common_id"])
        log(
            f"[pub {i}/{len(targets)} ok={n_ok}/{target}] "
            f"src={row.get('source')} sid={sid} {(row.get('title') or '')[:40]}"
        )
        t0 = time.time()
        one: dict = {
            "source_id": sid,
            "common_id": common_id,
            "link": row.get("link"),
            "origin": row.get("source"),
            "record_id": row.get("record_id"),
            "shop_id": SHOP_ID,
            "shop_name": SHOP_NAME,
            "ok": False,
        }
        try:
            public_item = {
                "commonCollectBoxDetailId": common_id,
                "itemNum": sid,
                "status": "success",
            }
            detail_id = claim_to_detail(client, public_item, SHOP_ID, sid, log)
            one["detail_id"] = detail_id
            ep_res = edit_and_publish_one(
                client,
                detail_id=detail_id,
                shop_id=SHOP_ID,
                shop_name=SHOP_NAME,
                scheme=scheme,
                publish_cfg=publish_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                log=log,
            )
            one.update(ep_res)
            # 回写飞书主表（仅有 record_id 的）
            rid = row.get("record_id")
            if rid:
                try:
                    hold = ep_res.get("hold_without_publish") or {}
                    if ep_res.get("ok"):
                        feishu.update_record(
                            rid,
                            {
                                inbound_f: yes,
                                fcfg.get("publish_status_field") or "上架状态": yes,
                                reason_f: "已上架",
                                shop_f: SHOP_NAME,
                                fcfg.get("detail_id_field") or "妙手detailId": str(detail_id),
                            },
                        )
                    elif ep_res.get("held_in_box"):
                        reason = str(
                            hold.get("reason")
                            or (ep_res.get("publish") or {}).get("reason")
                            or "门禁留库"
                        )
                        feishu.update_record(
                            rid,
                            {
                                inbound_f: yes,
                                fcfg.get("publish_status_field") or "上架状态": no,
                                reason_f: reason,
                                shop_f: SHOP_NAME,
                                fcfg.get("detail_id_field") or "妙手detailId": str(detail_id),
                            },
                        )
                    else:
                        feishu.update_record(
                            rid,
                            {
                                inbound_f: yes,
                                fcfg.get("publish_status_field") or "上架状态": no,
                                reason_f: "发布失败",
                                shop_f: SHOP_NAME,
                                fcfg.get("detail_id_field") or "妙手detailId": str(detail_id),
                            },
                        )
                except Exception as e2:
                    log(f"  飞书回写失败: {e2}")

            if ep_res.get("held_in_box"):
                n_hold += 1
                log(f"  留库 {time.time()-t0:.1f}s")
            elif ep_res.get("ok"):
                n_ok += 1
                quota.add(SHOP_ID, 1)
                log(f"  OK 已上架 {time.time()-t0:.1f}s detail={detail_id} ({n_ok}/{target})")
            else:
                n_fail += 1
                log(f"  FAIL publish {time.time()-t0:.1f}s")
        except Exception as e:
            n_fail += 1
            one["error"] = str(e)
            log(f"  FAIL {time.time()-t0:.1f}s {e}")
            rid = row.get("record_id")
            if rid:
                try:
                    err = str(e)
                    if len(err) > 200:
                        err = err[:200] + "…"
                    feishu.update_record(
                        rid,
                        {inbound_f: no, reason_f: err, shop_f: SHOP_NAME},
                    )
                except Exception:
                    pass
        results.append(one)
        result_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(0.8)

    summary = {
        "target_ok": target,
        "feishu_collect_ok": len(feishu_ok),
        "fail_table_ok": len(fail_ok),
        "pool": len(targets),
        "ok": n_ok,
        "held": n_hold,
        "fail": n_fail,
        "quota_han": quota.count(SHOP_ID),
        "shop": SHOP_NAME,
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"han_collect_then_publish_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if n_ok >= target else 1


if __name__ == "__main__":
    raise SystemExit(main())
