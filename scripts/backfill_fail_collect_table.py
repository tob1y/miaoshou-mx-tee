# -*- coding: utf-8 -*-
"""把主表里已有、但未进「采集失败链接」表的采集失败，补写入失败表。

默认：今天（UTC+8）有改动的主表记录；可加 --all 扫全表。
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from services.auto_batch import is_collect_fail_reason, write_collect_fail_record
from services.feishu_bitable import FeishuBitable
from services.link_resolve import extract_url_field

TZ = timezone(timedelta(hours=8))


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or v.get("value") or "")
    if isinstance(v, list):
        return " ".join(_text(x) for x in v)
    return str(v)


def main() -> int:
    scan_all = "--all" in sys.argv
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    fcfg = dict(settings.get("feishu") or {})
    feishu = FeishuBitable(
        str(fcfg["app_id"]),
        str(fcfg["app_secret"]),
        str(fcfg["bitable_app_token"]),
        str(fcfg["bitable_table_id"]),
    )
    fail_box = FeishuBitable(
        str(fcfg["app_id"]),
        str(fcfg["app_secret"]),
        str(fcfg["fail_collect_app_token"]),
        str(fcfg["fail_collect_table_id"]),
    )
    fail_box._token = feishu._token
    fail_box._token_expire_at = feishu._token_expire_at

    link_f = fcfg.get("link_field") or "上传链接"
    reason_f = fcfg.get("hold_reason_field") or "门禁原因"
    shop_f = fcfg.get("shop_field") or "分配店铺"
    flink = fcfg.get("fail_collect_link_field") or "上传链接"
    fsrc = fcfg.get("fail_collect_source_field") or "货源ID"
    freason = fcfg.get("fail_collect_reason_field") or "失败原因"

    print("读取采集失败表…", flush=True)
    fail_recs = fail_box.list_records(page_size=500)
    exist_sid: set[str] = set()
    exist_link: set[str] = set()
    exist_rec: set[str] = set()
    for it in fail_recs:
        fields = it.get("fields") or {}
        sid = _text(fields.get(fsrc)).strip()
        link = extract_url_field(fields.get(flink)) or ""
        if sid:
            exist_sid.add(sid)
        if link:
            exist_link.add(link.rstrip("/"))
        # also sid buried in reason
        mm = re.search(r"(1\d{15,})", _text(fields.get(freason)))
        if mm:
            exist_sid.add(mm.group(1))
    print(f"失败表已有 {len(fail_recs)} 条（sid={len(exist_sid)} link={len(exist_link)}）", flush=True)

    print("读取主飞书表…", flush=True)
    main_recs = feishu.list_records(page_size=500)
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today0_ms = int(today0.timestamp() * 1000)

    candidates: list[dict] = []
    for it in main_recs:
        fields = it.get("fields") or {}
        reason = _text(fields.get(reason_f))
        if not is_collect_fail_reason(reason):
            continue
        # last_modified_time from automatic fields (ms)
        lm = it.get("last_modified_time") or it.get("last_modified_time") 
        try:
            lm_ms = int(it.get("last_modified_time") or 0)
        except Exception:
            lm_ms = 0
        if not scan_all and lm_ms and lm_ms < today0_ms:
            continue
        link = extract_url_field(fields.get(link_f)) or ""
        rid = str(it.get("record_id") or it.get("id") or "")
        sid = ""
        mm = re.search(r"(1\d{15,})", reason)
        if mm:
            sid = mm.group(1)
        shop = _text(fields.get(shop_f))
        candidates.append(
            {
                "link": link,
                "reason": reason,
                "source_id": sid,
                "shop_name": shop,
                "record_id": rid,
                "fail_time_ms": lm_ms or int(time.time() * 1000),
            }
        )

    print(f"主表采集失败候选 {len(candidates)}（{'全表' if scan_all else '今天改过'}）", flush=True)

    # also harvest from today's batch logs (source_id + link when possible)
    log_dir = ROOT / "data/previews"
    log_extra = 0
    for logp in sorted(log_dir.glob("feishu_*20260917*.log")):
        text = logp.read_text(encoding="utf-8", errors="replace")
        # FAIL lines with 短链/公共箱
        for m in re.finditer(
            r"FAIL[^\n]*(?:短链解析失败|采集失败|公共箱无 success)[^\n]*",
            text,
        ):
            line = m.group(0)
            if not is_collect_fail_reason(line):
                continue
            link_m = re.search(r"(https://vt\.tiktok\.com/[^\s\)\"]+)", line)
            sid_m = re.search(r"(1\d{15,})", line)
            link = (link_m.group(1) if link_m else "").rstrip("/")
            sid = sid_m.group(1) if sid_m else ""
            shop = "小赵1店" if "zhao" in logp.name else "小韩1店"
            if (sid and sid in exist_sid) or (link and link in exist_link):
                continue
            # avoid dup with candidates
            if any(
                (sid and c.get("source_id") == sid) or (link and c.get("link", "").rstrip("/") == link)
                for c in candidates
            ):
                continue
            if not link and not sid:
                continue
            candidates.append(
                {
                    "link": link,
                    "reason": line[:500],
                    "source_id": sid,
                    "shop_name": shop,
                    "record_id": "",
                    "fail_time_ms": int(time.time() * 1000),
                    "from_log": logp.name,
                }
            )
            log_extra += 1
    print(f"日志额外候选 +{log_extra}", flush=True)

    # Also: from_row / zhao_batch — reconstruct failed sids from parse lines vs collect ok
    # Pull links from main by matching source in reason already covered; for pure sid from log
    # without main reason (unlikely) we still want them.
    for logp, shop in [
        (log_dir / "feishu_from_row_han_20260917_194654.log", "小韩1店"),
        (log_dir / "feishu_from_row_han_20260917_203252.log", "小韩1店"),
        (log_dir / "feishu_zhao_batch_20260917_182450.log", "小赵1店"),
    ]:
        if not logp.exists():
            continue
        text = logp.read_text(encoding="utf-8", errors="replace")
        # map: need link. From "取到" we don't have all. Use PhaseB ok sids and all parse sids.
        all_sid = re.findall(r"解析 \[\d+/\d+\] source=(\d+)", text)
        ok_sid = set(re.findall(r"origin=feishu sid=(\d+)", text))
        # zhao batch: successes in json
        if "zhao_batch" in logp.name:
            jp = log_dir / "feishu_zhao_batch_20260917_182450.json"
            if jp.exists():
                import json

                ok_sid |= {
                    str(x.get("source_id") or "")
                    for x in json.loads(jp.read_text(encoding="utf-8"))
                    if x.get("source_id")
                }
            # also "已有success" still collect ok
            ok_sid |= set(
                re.findall(r"解析 \[\d+/\d+\] source=(\d+) \(已有success\)", text)
            )
        else:
            ok_sid |= set(
                re.findall(r"解析 \[\d+/\d+\] source=(\d+) \(已有success\)", text)
            )
            # feishu collect ok may exceed PhaseB pool if extras; use 批量采集结束
            # Safer: fail = all parse without 已有success that aren't in pub feishu origin
            # Actually collect ok includes already-success; fail = those marked error in batch
            # Approximate: sid parsed without (已有success) and not in ok_sid from pub
            pass
        # Better fail set: sids that appear in 解析 but never as success in pub / already
        already = set(
            re.findall(r"解析 \[\d+/\d+\] source=(\d+) \(已有success\)", text)
        )
        fail_sids = [s for s in all_sid if s not in ok_sid and s not in already]
        # For zhao batch, ok_sid from json includes all collect+publish processed = collect ok only
        # collect_ok=75, json len=75 — good. already subset of collect ok.
        # fail = all_sid - those in json - wait all_sid includes already. 
        # collect fail ≈ all unique all_sid - ok from json - but already are in ok json? already means skip fetch but ok.
        if "zhao_batch" in logp.name:
            fail_sids = [s for s in dict.fromkeys(all_sid) if s not in ok_sid]
        else:
            # from_row: pool feishu origin = collect successes that entered PhaseB
            # already-success also collect ok but may be in pool
            collect_ok = ok_sid | already
            fail_sids = [s for s in dict.fromkeys(all_sid) if s not in collect_ok]

        # map sid -> link from main table candidates / main records
        sid_to_link: dict[str, str] = {}
        sid_to_rec: dict[str, str] = {}
        for it in main_recs:
            fields = it.get("fields") or {}
            reason = _text(fields.get(reason_f))
            mm = re.search(r"(1\d{15,})", reason)
            if not mm:
                continue
            sid_to_link[mm.group(1)] = extract_url_field(fields.get(link_f)) or ""
            sid_to_rec[mm.group(1)] = str(it.get("record_id") or "")

        added = 0
        for sid in fail_sids:
            if sid in exist_sid:
                continue
            if any(c.get("source_id") == sid for c in candidates):
                continue
            link = sid_to_link.get(sid) or ""
            if link and link.rstrip("/") in exist_link:
                continue
            candidates.append(
                {
                    "link": link,
                    "reason": f"采集失败(提前结束): 公共箱无 success: {sid}",
                    "source_id": sid,
                    "shop_name": shop,
                    "record_id": sid_to_rec.get(sid) or "",
                    "fail_time_ms": int(time.time() * 1000),
                    "from_log": logp.name,
                }
            )
            added += 1
        print(f"  {logp.name}: fail_sids≈{len(fail_sids)} new_candidates+{added}", flush=True)

    # dedupe candidates
    uniq: list[dict] = []
    seen: set[str] = set()
    for c in candidates:
        sid = str(c.get("source_id") or "")
        link = str(c.get("link") or "").rstrip("/")
        key = sid or link or c.get("record_id") or ""
        if not key:
            continue
        if sid and sid in exist_sid:
            continue
        if link and link in exist_link:
            continue
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    print(f"待写入（去重且失败表没有）: {len(uniq)}", flush=True)
    ok = fail = 0
    for i, c in enumerate(uniq, 1):
        link = c.get("link") or ""
        if not link and c.get("source_id"):
            # still write with empty link? Better skip empty link — user needs link to retry
            # try keep writing with reason containing sid
            pass
        if not link:
            # skip no-link — can't manually retry easily; still write if sid present using placeholder?
            # User asked for fail links doc — skip empty link
            print(f"  skip no-link sid={c.get('source_id')}", flush=True)
            fail += 1
            continue
        ok_w = write_collect_fail_record(
            feishu,
            fcfg,
            link=link,
            reason=str(c.get("reason") or "采集失败"),
            source_id=str(c.get("source_id") or ""),
            shop_name=str(c.get("shop_name") or ""),
            record_id=str(c.get("record_id") or ""),
            fail_time_ms=int(c.get("fail_time_ms") or time.time() * 1000),
        )
        if ok_w:
            ok += 1
            if c.get("source_id"):
                exist_sid.add(str(c["source_id"]))
            exist_link.add(link.rstrip("/"))
            if i % 20 == 0 or i == len(uniq):
                print(f"  进度 {i}/{len(uniq)} written={ok}", flush=True)
        else:
            fail += 1
        time.sleep(0.15)

    print(f"完成: written={ok} skipped/fail={fail} fail_table_was={len(fail_recs)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
