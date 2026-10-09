# -*- coding: utf-8 -*-
"""飞书从指定行号起取 100 条批量采集 + 合并公共箱额外 success → 上架小韩。"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.auto_batch import (
    _field_is_yes,
    _field_text,
    batch_collect_rows,
    claim_to_detail,
    edit_and_publish_one,
    qps_retry,
    write_collect_fail_record,
)
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable
from services.link_resolve import extract_url_field

SHOP_ID = 18545217
SHOP_NAME = "小韩1店"
TZ = timezone(timedelta(hours=8))
# 用户：跑到约 1065，从 1066 开始
START_ROW = 1066  # 1-based，按创建时间旧→新
BATCH_N = 100
# 额外 10 条：手动采、约 19:39 的 success
EXTRA_SINCE = datetime(2026, 9, 17, 19, 39, 0, tzinfo=TZ)


def parse_gmt(gmt: str, tz=TZ):
    gmt = str(gmt or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(gmt[:26], fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def feishu_rows_from(records: list[dict], fcfg: dict, start_row: int, n: int) -> list[dict]:
    """按创建时间旧→新编号，从 start_row 起取有链接且未上架的最多 n 条。"""
    link_f = fcfg.get("link_field") or "上传链接"
    pub_f = fcfg.get("publish_status_field") or "上架状态"
    yes = fcfg.get("yes_value") or "是"
    ordered = sorted(records, key=lambda r: int(r.get("created_time") or 0))
    out: list[dict] = []
    for i, it in enumerate(ordered, 1):
        if i < start_row:
            continue
        fields = it.get("fields") or {}
        link = extract_url_field(fields.get(link_f))
        if not link:
            continue
        st = fields.get(pub_f)
        if st == yes or (isinstance(st, dict) and st.get("name") == yes):
            continue
        out.append(
            {
                "record_id": it.get("record_id") or it.get("id"),
                "link": link,
                "fields": fields,
                "created_time": int(it.get("created_time") or 0),
                "ui_row": i,
            }
        )
        if len(out) >= n:
            break
    return out


def list_extra_public_success(client: MiaoshouClient, since: datetime, limit: int = 20) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, 10):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "public",
            ),
            "public_list",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            if str(it.get("status") or "").lower() != "success":
                continue
            dt = parse_gmt(str(it.get("gmtCreate") or ""))
            if not dt or dt < since:
                continue
            cid = it.get("commonCollectBoxDetailId")
            sid = str(it.get("itemNum") or it.get("sourceItemId") or "")
            if cid is None or not sid:
                continue
            rows.append(
                {
                    "source": "public_extra",
                    "common_id": int(cid),
                    "sid": sid,
                    "title": str(it.get("title") or "")[:80],
                    "gmt": str(it.get("gmtCreate") or ""),
                    "public_item": it,
                }
            )
        last = parse_gmt(str(batch[-1].get("gmtCreate") or ""))
        first = parse_gmt(str(batch[0].get("gmtCreate") or ""))
        if last and first and first >= last and last < since:
            break
        if len(batch) < 100:
            break
        time.sleep(0.3)
    uniq: dict[int, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("gmt") or ""):
        uniq[r["common_id"]] = r
    return list(uniq.values())[:limit]


def main() -> int:
    start_row = START_ROW
    batch_n = BATCH_N
    if len(sys.argv) > 1:
        start_row = int(sys.argv[1])
    if len(sys.argv) > 2:
        batch_n = int(sys.argv[2])

    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = dict(settings.get("feishu") or {})
    m = settings.get("miaoshou") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"feishu_from_row_han_{stamp}.log"
    result_path = out_dir / f"feishu_from_row_han_{stamp}.json"

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

    log(f"飞书从第 {start_row} 行起取 {batch_n} 条 → 批量采集；另合并 19:39 后公共箱 success；上架 {SHOP_NAME}")
    records = feishu.list_records(page_size=500)
    log(f"飞书总记录 {len(records)}")
    batch = feishu_rows_from(records, fcfg, start_row, batch_n)
    if batch:
        log(
            f"取到 {len(batch)} 条：UI行 {batch[0].get('ui_row')}→{batch[-1].get('ui_row')} "
            f"队首 {batch[0]['link'][:50]}"
        )
    else:
        log("从该行起没有可取链接")
        return 1

    # Phase A: batch collect Feishu links
    log(f"=== PhaseA 批量采集 {len(batch)} 条 ===")
    collected = batch_collect_rows(client, batch, log, fetch_chunk=50)

    no = fcfg.get("no_value") or "否"
    inbound_f = fcfg.get("inbound_status_field") or "入库状态"
    reason_f = fcfg.get("hold_reason_field") or "门禁原因"
    shop_f = fcfg.get("shop_field") or "分配店铺"
    yes = fcfg.get("yes_value") or "是"
    pub_f = fcfg.get("publish_status_field") or "上架状态"
    detail_f = fcfg.get("detail_id_field") or "妙手detailId"

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
    log(f"飞书采集成功 {len(feishu_ok)} / {len(batch)}")

    # Phase A2: 可选合并公共箱额外 success（续跑时默认关掉，避免重复认领）
    merge_extra = "--extra" in sys.argv
    if merge_extra:
        log("=== 拉取 19:39 后公共箱额外 success ===")
        extras = list_extra_public_success(client, EXTRA_SINCE, limit=30)
        log(f"额外 success {len(extras)} 条")
    else:
        extras = []
        log("跳过公共箱额外合并（需要时加 --extra）")

    # merge publish pool (sid dedupe; feishu first)
    pool: list[dict] = []
    seen_sid: set[str] = set()
    for x in feishu_ok:
        sid = str(x.get("source_id") or "")
        if not sid or sid in seen_sid:
            continue
        seen_sid.add(sid)
        pool.append(
            {
                "origin": "feishu",
                "sid": sid,
                "public_item": x["public_item"],
                "row": x["row"],
                "record_id": x["row"].get("record_id"),
            }
        )
    for x in extras:
        sid = str(x.get("sid") or "")
        if not sid or sid in seen_sid:
            continue
        seen_sid.add(sid)
        pool.append(
            {
                "origin": "public_extra",
                "sid": sid,
                "public_item": {
                    "commonCollectBoxDetailId": x["common_id"],
                    "itemNum": sid,
                    "status": "success",
                },
                "row": None,
                "record_id": None,
                "title": x.get("title"),
            }
        )

    log(f"=== PhaseB 合并上架池 {len(pool)} → {SHOP_NAME} ===")
    results = []
    ok = held = fail = 0
    for i, item in enumerate(pool, 1):
        if quota.remaining(SHOP_ID) <= 0:
            log("配额用尽")
            break
        sid = item["sid"]
        log(f"[pub {i}/{len(pool)}] origin={item['origin']} sid={sid}")
        t0 = time.time()
        one: dict = {"source_id": sid, "origin": item["origin"], "ok": False}
        try:
            detail_id = claim_to_detail(client, item["public_item"], SHOP_ID, sid, log)
            one["detail_id"] = detail_id
            ep = edit_and_publish_one(
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
            one.update(ep)
            rid = item.get("record_id")
            if rid:
                try:
                    if ep.get("ok"):
                        feishu.update_record(
                            rid,
                            {
                                inbound_f: yes,
                                pub_f: yes,
                                reason_f: "已上架",
                                shop_f: SHOP_NAME,
                                detail_f: str(detail_id),
                            },
                        )
                    elif ep.get("held_in_box"):
                        hold = ep.get("hold_without_publish") or {}
                        reason = str(
                            hold.get("reason")
                            or (ep.get("publish") or {}).get("reason")
                            or "门禁留库"
                        )
                        feishu.update_record(
                            rid,
                            {
                                inbound_f: yes,
                                pub_f: no,
                                reason_f: reason,
                                shop_f: SHOP_NAME,
                                detail_f: str(detail_id),
                            },
                        )
                    else:
                        feishu.update_record(
                            rid,
                            {
                                inbound_f: yes,
                                pub_f: no,
                                reason_f: "发布失败",
                                shop_f: SHOP_NAME,
                                detail_f: str(detail_id),
                            },
                        )
                except Exception as e2:
                    log(f"  飞书回写失败: {e2}")

            if ep.get("held_in_box"):
                held += 1
                log(f"  留库 {time.time()-t0:.1f}s")
            elif ep.get("ok"):
                ok += 1
                quota.add(SHOP_ID, 1)
                log(f"  OK 已上架 {time.time()-t0:.1f}s detail={detail_id} ({ok})")
            else:
                fail += 1
                log(f"  FAIL publish {time.time()-t0:.1f}s")
        except Exception as e:
            fail += 1
            one["error"] = str(e)
            log(f"  FAIL {time.time()-t0:.1f}s {e}")
            rid = item.get("record_id")
            if rid:
                try:
                    err = str(e)
                    if len(err) > 200:
                        err = err[:200] + "…"
                    feishu.update_record(rid, {inbound_f: no, reason_f: err, shop_f: SHOP_NAME})
                except Exception:
                    pass
        results.append(one)
        result_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(0.5)

    summary = {
        "start_row": start_row,
        "batch_selected": len(batch),
        "feishu_collect_ok": len(feishu_ok),
        "public_extra": len(extras),
        "pool": len(pool),
        "ok": ok,
        "held": held,
        "fail": fail,
        "shop": SHOP_NAME,
        "quota_han": quota.count(SHOP_ID),
        "ui_row_range": [batch[0].get("ui_row"), batch[-1].get("ui_row")] if batch else [],
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"feishu_from_row_han_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
