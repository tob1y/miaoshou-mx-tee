"""Auto batch: Feishu pending links → collect → split shops → edit → publish."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable
from services.full_pipeline import claim_public_to_shop, qps_retry, wait_shop_detail
from services.link_resolve import extract_url_field, resolve_tiktok_link
from services.publish_prep import apply_publish_prep
from services.publisher import publish_details
from services.transformer import transform_product

LogFn = Callable[[str], None]

COLLECT_FAIL_MARKERS = (
    "短链解析失败",
    "无法解析商品ID",
    "采集失败",
    "公共箱无 success",
    "死链",
)


def is_collect_fail_reason(reason: str) -> bool:
    return any(k in str(reason or "") for k in COLLECT_FAIL_MARKERS)


def write_collect_fail_record(
    feishu: FeishuBitable,
    fcfg: dict,
    *,
    link: str,
    reason: str,
    source_id: str = "",
    shop_name: str = "",
    record_id: str = "",
    fail_time_ms: int | None = None,
    log: LogFn | None = None,
) -> bool:
    """写入独立「采集失败链接」表。成功返回 True。"""
    _log = log or (lambda _m: None)
    app_token = str(fcfg.get("fail_collect_app_token") or "").strip()
    table_id = str(fcfg.get("fail_collect_table_id") or "").strip()
    if not app_token or not table_id:
        return False
    try:
        fail_box = FeishuBitable(
            feishu.app_id,
            feishu.app_secret,
            app_token,
            table_id,
        )
        fail_box._token = feishu._token
        fail_box._token_expire_at = feishu._token_expire_at
        link_f = fcfg.get("fail_collect_link_field") or "上传链接"
        reason_ff = fcfg.get("fail_collect_reason_field") or "失败原因"
        source_f = fcfg.get("fail_collect_source_field") or "货源ID"
        shop_ff = fcfg.get("fail_collect_shop_field") or "分配店铺"
        time_f = fcfg.get("fail_collect_time_field") or "失败时间"
        rec_f = fcfg.get("fail_collect_record_field") or "来源记录ID"
        msg = reason if len(reason) <= 500 else reason[:500] + "…"
        fail_box.create_record(
            {
                link_f: link,
                reason_ff: msg,
                source_f: str(source_id or ""),
                shop_ff: str(shop_name or ""),
                time_f: int(fail_time_ms if fail_time_ms is not None else time.time() * 1000),
                rec_f: str(record_id or ""),
            }
        )
        _log("已写入采集失败表")
        return True
    except Exception as e3:
        _log(f"采集失败表写入失败: {e3}")
        return False


def list_public_by_source(client: MiaoshouClient, source_id: str) -> list[dict]:
    body = qps_retry(
        lambda: client.assert_success(
            client.post(
                ep.PUBLIC_LIST,
                {"pageNo": 1, "pageSize": 50, "filter": {"sourceItemIdKeyword": str(source_id)}},
            ),
            "公共箱源ID",
        ),
        "public_kw",
    )
    return (body.get("data") or {}).get("detailList") or []


def pick_success_public(items: list[dict], source_id: str) -> dict | None:
    """Prefer status=success; ignore skip duplicates (产品已经采集过)."""
    matched = []
    for it in items:
        item_num = str(it.get("itemNum") or "")
        sources = it.get("sourceList") or []
        sid_ok = item_num == source_id or any(
            str((s or {}).get("sourceItemId") or "") == source_id
            for s in sources
            if isinstance(s, dict)
        )
        if sid_ok and str(it.get("status") or "").lower() == "success":
            matched.append(it)
    if not matched:
        return None
    matched.sort(key=lambda x: str(x.get("gmtCreate") or ""))
    return matched[0]


def ensure_collected(client: MiaoshouClient, source_id: str, collect_url: str, log: LogFn) -> dict:
    items = list_public_by_source(client, source_id)
    hit = pick_success_public(items, source_id)
    if hit:
        return hit
    log(f"采集 {collect_url}")
    try:
        client.fetch_item([collect_url])
    except Exception as e:
        # Bugfix: gateway 504 may still enqueue; keep polling success row
        log(f"fetch warn: {e}")
    # 正常 几秒内 success；采不到就快失败，避免单条空等近 3 分钟
    for attempt in range(1, 9):
        items = list_public_by_source(client, source_id)
        hit = pick_success_public(items, source_id)
        if hit:
            return hit
        # 若只有 skip/失败记录，不必空等到底
        if items and not any(str(it.get("status") or "").lower() == "success" for it in items):
            statuses = sorted({str(it.get("status") or "") for it in items})
            if attempt >= 3 and statuses and "success" not in {s.lower() for s in statuses}:
                log(f"公共箱无 success（已见状态 {statuses}），提前结束 source={source_id}")
                break
        log(f"等待 success #{attempt} source={source_id}")
        time.sleep(2)
    raise MiaoshouError(f"公共箱无 success: {source_id}")


def find_tiktok_detail_id(client: MiaoshouClient, source_id: str) -> int | None:
    body = client.search_tiktok_box(page_no=1, page_size=50, status=None, source_item_id=source_id)
    for it in (body.get("data") or {}).get("detailList") or []:
        try:
            return int(it.get("collectBoxDetailId"))
        except Exception:
            continue
    return None


def claim_to_detail(
    client: MiaoshouClient,
    public_item: dict,
    shop_id: int,
    source_id: str,
    log: LogFn,
) -> int:
    """Claim public → shop detailId; fallback if already claimed elsewhere."""
    mapping = {}
    try:
        mapping = claim_public_to_shop(client, [public_item], shop_id, log=log)
    except Exception as e:
        log(f"claim_public warn: {e}")
    common_id = int(public_item["commonCollectBoxDetailId"])
    detail_id = mapping.get(common_id)
    if detail_id:
        qps_retry(lambda: client.claim_to_shops([detail_id], [shop_id]), "claim_to_shop", log=log)
        return int(detail_id)

    # Bugfix: empty mapping when already claimed to another shop
    existing = find_tiktok_detail_id(client, source_id)
    if not existing:
        time.sleep(1.2)
        existing = find_tiktok_detail_id(client, source_id)
    if not existing:
        raise MiaoshouError(f"认领无 detailId source={source_id} common={common_id}")
    log(f"复用已有 TikTok detailId={existing} → shop={shop_id}")
    qps_retry(lambda: client.claim_to_shops([existing], [shop_id]), "claim_to_shop_reuse", log=log)
    return int(existing)


def edit_and_publish_one(
    client: MiaoshouClient,
    *,
    detail_id: int,
    shop_id: int,
    shop_name: str,
    scheme: dict,
    publish_cfg: dict,
    blank_dir: Path,
    fill_urls: list[str],
    log: LogFn,
    vision_cfg: dict | None = None,
    skip_reclaim: bool = False,
) -> dict[str, Any]:
    pub_cfg = dict(publish_cfg)
    pub_cfg["shop_id"] = int(shop_id)
    pub_cfg["shop_name"] = shop_name
    # Bugfix: same detail published to shop A must still publish to shop B
    anti = dict(pub_cfg.get("anti_duplicate") or {})
    anti["filter_published"] = False
    pub_cfg["anti_duplicate"] = anti

    timing: dict[str, float] = {}
    t_all = time.time()

    t0 = time.time()
    if skip_reclaim:
        timing["reclaim_s"] = 0.0
        timing["wait_after_reclaim_s"] = 0.0
    else:
        qps_retry(lambda: client.claim_to_shops([detail_id], [shop_id]), "reclaim", log=log)
        timing["reclaim_s"] = round(time.time() - t0, 3)
        time.sleep(0.5)
        timing["wait_after_reclaim_s"] = 0.5

    t0 = time.time()
    oss, product = wait_shop_detail(client, detail_id, shop_id, log=log)
    timing["get_detail_s"] = round(time.time() - t0, 3)

    t0 = time.time()
    report = transform_product(
        product,
        scheme,
        blank_dir,
        fill_image_urls=fill_urls,
        vision_cfg=vision_cfg,
    )
    timing["transform_s"] = round(time.time() - t0, 3)
    # 拆分：文字门禁 / 识图 / 改品
    hold0 = report.get("hold_without_publish") or {}
    vision0 = hold0.get("vision") or {}
    if isinstance(vision0, dict) and vision0.get("elapsed_s") is not None:
        timing["vision_s"] = float(vision0.get("elapsed_s") or 0)
    timing["text_and_mutate_s"] = round(
        max(0.0, timing["transform_s"] - float(timing.get("vision_s") or 0)), 3
    )
    report["product"].pop("_pending_local_images", None)

    hold = report.get("hold_without_publish") or {}
    reason = str(hold.get("reason") or "")
    reasons = set(hold.get("reasons") or [])
    # 错品类 / SKU 崩了：不保存（避免硬套男士T恤或撞「SKU必填」）
    skip_save = bool(
        reasons
        & {
            "wrong_category_or_audience",
            "compression_or_tight_fit",
            "no_active_sku",
            "sku_map_orphan",
            "multipack",
            "long_sleeve_only",
            "hoodie",
            "contrast_sleeves",
            "random_assortment",
            "no_color",
            "vision_unclear",
            "long_sleeve",
            "multipack_vision",
            "multipack_option_name",
            "main_no_library_color_left",
            "options_no_library_color_left",
        }
    ) or any(
        x in reason
        for x in (
            "wrong_category_or_audience",
            "compression_or_tight_fit",
            "no_active_sku",
            "sku_map_orphan",
            "multipack",
            "long_sleeve",
            "hoodie",
            "contrast_sleeves",
            "random_assortment",
            "vision_unclear",
            "non_pure_black",
            "garment_",
            "main_no_library",
            "options_no_library",
            "multipack_option",
            "multipack_vision",
        )
    ) or reason in ("no_color", "missing_color", "no_publishable_color", "title_and_options_no_concrete_color")

    if hold.get("hold") and skip_save:
        timing["total_s"] = round(time.time() - t_all, 3)
        log(f"留库不上架 detail={detail_id}: {reason}（未保存改品）")
        return {
            "ok": False,
            "held_in_box": True,
            "detail_id": detail_id,
            "prep": None,
            "save_skipped": True,
            "hold_without_publish": hold,
            "publish": {"skipped": True, "reason": reason},
            "timing": timing,
        }

    t0 = time.time()
    prep = apply_publish_prep(report["product"], pub_cfg)
    timing["publish_prep_s"] = round(time.time() - t0, 3)

    def _save_once() -> dict:
        # 并发改品冲突时：重新读再存
        nonlocal oss
        try:
            return client.save_shop_detail(oss, report["product"], detail_id, shop_id)
        except MiaoshouError as e:
            if "产品数据发生变动" not in str(e) and "发生变动" not in str(e):
                raise
            log(f"  改品并发冲突，重读再存 detail={detail_id}")
            time.sleep(1.2)
            oss, product2 = wait_shop_detail(client, detail_id, shop_id, log=log)
            # 用最新 oss，仍提交我们改好的 product（字段以改品结果为准）
            return client.save_shop_detail(oss, report["product"], detail_id, shop_id)

    t0 = time.time()
    save = qps_retry(_save_once, "save", log=log)
    timing["save_s"] = round(time.time() - t0, 3)
    time.sleep(0.6)
    timing["wait_after_save_s"] = 0.6

    if hold.get("hold"):
        timing["total_s"] = round(time.time() - t_all, 3)
        log(f"留库不上架 detail={detail_id}: {reason}")
        return {
            "ok": False,
            "held_in_box": True,
            "detail_id": detail_id,
            "prep": prep,
            "save_code": save.get("code") if isinstance(save, dict) else None,
            "hold_without_publish": hold,
            "publish": {"skipped": True, "reason": reason},
            "timing": timing,
        }

    t0 = time.time()
    pub = publish_details(
        client,
        [detail_id],
        pub_cfg,
        dry_run=False,
        confirm=True,
        prep_before_publish=False,
    )
    timing["publish_api_s"] = round(time.time() - t0, 3)
    timing["total_s"] = round(time.time() - t_all, 3)
    calls = pub.get("publish_calls") or []
    pub_ok = bool(calls) and all(c.get("ok") for c in calls)
    return {
        "ok": pub_ok,
        "detail_id": detail_id,
        "prep": prep,
        "save_code": save.get("code") if isinstance(save, dict) else None,
        "hold_without_publish": hold,
        "publish": pub,
        "timing": timing,
    }


def _field_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        return str(val.get("name") or val.get("text") or "")
    if isinstance(val, list):
        parts = [_field_text(x) for x in val]
        return "".join(p for p in parts if p)
    return str(val).strip()


def _field_is_yes(val: Any, yes: str = "是") -> bool:
    if val == yes:
        return True
    if isinstance(val, dict) and val.get("name") == yes:
        return True
    return False


def pending_rows(records: list[dict], fcfg: dict) -> list[dict]:
    """未上架链接；按创建时间旧→新，便于「从第一个未尝试往下」取批。"""
    link_f = fcfg.get("link_field") or "上传链接"
    pub_f = fcfg.get("publish_status_field") or "上架状态"
    yes = fcfg.get("yes_value") or "是"
    out = []
    for it in records:
        fields = it.get("fields") or {}
        link = extract_url_field(fields.get(link_f))
        if not link:
            continue
        st = fields.get(pub_f)
        # already published
        if st == yes or (isinstance(st, dict) and st.get("name") == yes):
            continue
        out.append(
            {
                "record_id": it.get("record_id") or it.get("id"),
                "link": link,
                "fields": fields,
                "created_time": int(it.get("created_time") or 0),
            }
        )
    # 旧→新：开始节点=第一个未尝试，往下连续取一批
    out.sort(key=lambda r: int(r.get("created_time") or 0))
    return out


def fresh_pending_rows(records: list[dict], fcfg: dict) -> list[dict]:
    """未上架 + 未入库 + 门禁原因为空（从未尝试过）。

    约定：无论成功/失败/留库，process_one_row 都会回写「门禁原因」。
    下次启动从「第一个未尝试」往下取批（旧→新）。
    """
    inbound_f = fcfg.get("inbound_status_field") or "入库状态"
    reason_f = fcfg.get("hold_reason_field") or "门禁原因"
    yes = fcfg.get("yes_value") or "是"
    out = []
    for row in pending_rows(records, fcfg):
        fields = row.get("fields") or {}
        if _field_is_yes(fields.get(inbound_f), yes):
            continue
        # 有门禁原因 = 已尝试过（成功写「已上架」、失败写错误、留库写门禁码）
        if _field_text(fields.get(reason_f)):
            continue
        out.append(row)
    return out


def take_batch_from_start(pending: list[dict], batch_size: int) -> list[dict]:
    """从开始节点（第一个未尝试）往下取最多 batch_size 条；不足则有多少取多少。"""
    n = max(0, int(batch_size))
    return list(pending[:n])


def batch_collect_rows(
    client: MiaoshouClient,
    rows: list[dict],
    log: LogFn,
    *,
    fetch_chunk: int = 50,
    poll_rounds: int = 20,
    poll_sleep: float = 2.0,
) -> list[dict[str, Any]]:
    """整批采集：先解析短链，再一次/分块 fetch_item，最后统一轮询公共箱结果。

    返回与 rows 等长的列表，每项含：
      row, source_id, collect_url, ok, public_item?, error?
    """
    resolved: list[dict[str, Any]] = []
    for i, row in enumerate(rows, 1):
        link = row.get("link") or ""
        one: dict[str, Any] = {"row": row, "ok": False, "link": link}
        try:
            source_id, collect_url = resolve_tiktok_link(link)
            if not source_id:
                raise MiaoshouError(f"无法解析商品ID(死链/无效短链): {link}")
            one["source_id"] = str(source_id)
            one["collect_url"] = collect_url
            # 已有 success 则不必再 fetch
            hit = pick_success_public(list_public_by_source(client, str(source_id)), str(source_id))
            if hit:
                one["ok"] = True
                one["public_item"] = hit
                one["already"] = True
            resolved.append(one)
            log(f"  解析 [{i}/{len(rows)}] source={source_id}" + (" (已有success)" if hit else ""))
        except Exception as e:
            one["error"] = f"短链解析失败(提前结束): {link} ({e})"
            resolved.append(one)
            log(f"  解析失败 [{i}/{len(rows)}] {str(e)[:100]}")

    need_fetch = [x for x in resolved if not x.get("ok") and x.get("collect_url")]
    urls = [str(x["collect_url"]) for x in need_fetch]
    # 去重保序
    seen_u: set[str] = set()
    uniq_urls: list[str] = []
    for u in urls:
        if u not in seen_u:
            seen_u.add(u)
            uniq_urls.append(u)

    chunk = max(1, int(fetch_chunk))
    if uniq_urls:
        log(f"批量 fetch_item 共 {len(uniq_urls)} 条链接（chunk={chunk}）…")
        for i in range(0, len(uniq_urls), chunk):
            part = uniq_urls[i : i + chunk]
            try:
                client.fetch_item(part)
                log(f"  fetch ok chunk {i // chunk + 1}/{(len(uniq_urls) + chunk - 1) // chunk} size={len(part)}")
            except Exception as e:
                log(f"  fetch warn chunk {i // chunk + 1}: {e}")
            time.sleep(0.5)

    pending_ids = {
        str(x["source_id"]): x
        for x in resolved
        if not x.get("ok") and x.get("source_id")
    }
    if pending_ids:
        log(f"统一轮询公共箱 success，待确认 {len(pending_ids)} …")
    for attempt in range(1, poll_rounds + 1):
        if not pending_ids:
            break
        done_sids: list[str] = []
        for sid, item in list(pending_ids.items()):
            items = list_public_by_source(client, sid)
            hit = pick_success_public(items, sid)
            if hit:
                item["ok"] = True
                item["public_item"] = hit
                done_sids.append(sid)
                continue
            if items and attempt >= 4:
                statuses = sorted({str(it.get("status") or "") for it in items})
                low = {s.lower() for s in statuses}
                if "success" not in low and statuses:
                    # 已出现 fail/skip 等且无 success → 记失败，不再空等
                    if low & {"fail", "failed", "skip", "error"} or attempt >= 8:
                        item["error"] = f"采集失败(提前结束): 公共箱无 success: {sid}"
                        done_sids.append(sid)
        for sid in done_sids:
            pending_ids.pop(sid, None)
        if pending_ids:
            log(f"  轮询 #{attempt} 剩余 {len(pending_ids)}")
            time.sleep(poll_sleep)

    for sid, item in pending_ids.items():
        item["error"] = item.get("error") or f"采集失败(提前结束): 公共箱无 success: {sid}"

    ok_n = sum(1 for x in resolved if x.get("ok"))
    fail_n = len(resolved) - ok_n
    log(f"批量采集结束: ok={ok_n} fail={fail_n} / {len(resolved)}")
    return resolved



def split_batch(rows: list[dict], split_cfg: list[dict], quota: DailyQuota, batch_size: int) -> list[dict]:
    """50/50 assign, capped by each shop's daily remaining."""
    shops = [
        {
            "shop_id": int(s["shop_id"]),
            "shop_name": str(s.get("shop_name") or s["shop_id"]),
            "remain": quota.remaining(int(s["shop_id"])),
        }
        for s in split_cfg
    ]
    if len(shops) < 2:
        raise ValueError("feishu.split 需要两家店")

    take_n = min(len(rows), int(batch_size))
    # how many we can still place today across shops
    capacity = sum(s["remain"] for s in shops)
    take_n = min(take_n, capacity)
    if take_n <= 0:
        return []

    a_take = min(shops[0]["remain"], take_n // 2 + take_n % 2)
    b_take = min(shops[1]["remain"], take_n - a_take)
    # if one shop saturated, give leftover to the other
    if a_take + b_take < take_n:
        leftover = take_n - a_take - b_take
        if shops[0]["remain"] > a_take:
            extra = min(leftover, shops[0]["remain"] - a_take)
            a_take += extra
            leftover -= extra
        if leftover and shops[1]["remain"] > b_take:
            b_take += min(leftover, shops[1]["remain"] - b_take)

    assigned: list[dict] = []
    idx = 0
    for _ in range(a_take):
        if idx >= len(rows):
            break
        assigned.append({**rows[idx], **shops[0]})
        idx += 1
    for _ in range(b_take):
        if idx >= len(rows):
            break
        assigned.append({**rows[idx], **shops[1]})
        idx += 1
    return assigned


def process_one_row(
    client: MiaoshouClient,
    feishu: FeishuBitable,
    row: dict,
    *,
    fcfg: dict,
    scheme: dict,
    publish_cfg: dict,
    blank_dir: Path,
    fill_urls: list[str],
    quota: DailyQuota,
    log: LogFn,
    public_item: dict[str, Any] | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    yes = fcfg.get("yes_value") or "是"
    no = fcfg.get("no_value") or "否"
    inbound_f = fcfg.get("inbound_status_field") or "入库状态"
    inbound_t = fcfg.get("inbound_time_field") or "入库时间"
    publish_f = fcfg.get("publish_status_field") or "上架状态"
    publish_t = fcfg.get("publish_time_field") or "上架时间"
    shop_f = fcfg.get("shop_field") or "分配店铺"
    reason_f = fcfg.get("hold_reason_field") or "门禁原因"
    detail_f = fcfg.get("detail_id_field") or "妙手detailId"

    result: dict[str, Any] = {
        "record_id": row["record_id"],
        "link": row["link"],
        "shop_id": row["shop_id"],
        "shop_name": row["shop_name"],
        "ok": False,
    }
    now_ms = int(time.time() * 1000)

    def _feishu_write(fields: dict[str, Any]) -> None:
        try:
            feishu.update_record(row["record_id"], fields)
        except Exception as e2:
            log(f"飞书回写失败: {e2}")

    def _log_collect_fail(reason: str, source_id: str | None = None) -> None:
        write_collect_fail_record(
            feishu,
            fcfg,
            link=str(row.get("link") or ""),
            reason=reason,
            source_id=str(source_id or result.get("source_id") or ""),
            shop_name=str(row.get("shop_name") or ""),
            record_id=str(row.get("record_id") or ""),
            log=log,
        )

    try:
        if public_item is not None:
            if not source_id:
                raise MiaoshouError("预采集结果缺少 source_id")
            result["source_id"] = str(source_id)
        else:
            try:
                source_id, collect_url = resolve_tiktok_link(row["link"])
            except Exception as e:
                # 短链超时/网络失败：立刻飞书写否，不空耗批次
                raise MiaoshouError(f"短链解析失败(提前结束): {row['link']} ({e})") from e
            if not source_id:
                raise MiaoshouError(f"无法解析商品ID(死链/无效短链): {row['link']}")
            result["source_id"] = source_id
            try:
                public_item = ensure_collected(client, source_id, collect_url, log)
            except MiaoshouError as e:
                # 采集失败：保持提前失败，飞书入库/上架=否
                raise MiaoshouError(f"采集失败(提前结束): {e}") from e
        assert public_item is not None
        source_id = str(result["source_id"])
        result["common_id"] = public_item.get("commonCollectBoxDetailId")
        fields_in = {
            inbound_f: yes,
            inbound_t: now_ms,
            shop_f: row["shop_name"],
            reason_f: "",
        }
        _feishu_write(fields_in)

        detail_id = claim_to_detail(client, public_item, row["shop_id"], source_id, log)
        result["detail_id"] = detail_id
        ep_res = edit_and_publish_one(
            client,
            detail_id=detail_id,
            shop_id=row["shop_id"],
            shop_name=row["shop_name"],
            scheme=scheme,
            publish_cfg=publish_cfg,
            blank_dir=blank_dir,
            fill_urls=fill_urls,
            log=log,
        )
        result.update(ep_res)

        hold = ep_res.get("hold_without_publish") or {}
        hold_reason = ""
        if ep_res.get("held_in_box"):
            hold_reason = str(
                hold.get("reason")
                or (ep_res.get("publish") or {}).get("reason")
                or "门禁留库"
            )
        elif not ep_res.get("ok"):
            hold_reason = "发布失败"

        out_fields: dict[str, Any] = {
            publish_f: yes if ep_res.get("ok") else no,
            publish_t: int(time.time() * 1000),
            shop_f: row["shop_name"],
            detail_f: str(detail_id),
            reason_f: ("已上架" if ep_res.get("ok") else hold_reason),
        }
        _feishu_write(out_fields)
        if ep_res.get("ok"):
            quota.add(int(row["shop_id"]), 1)
        return result
    except Exception as e:
        result["error"] = str(e)
        log(f"FAIL {row.get('record_id')}: {e}")
        err = str(e)
        # 截断，避免飞书文本过长
        if len(err) > 200:
            err = err[:200] + "…"
        _feishu_write(
            {
                inbound_f: no,
                inbound_t: now_ms,
                publish_f: no,
                publish_t: int(time.time() * 1000),
                shop_f: row["shop_name"],
                reason_f: err,
                **({detail_f: str(result["detail_id"])} if result.get("detail_id") else {}),
            }
        )
        # 仅采集/短链类失败进独立表（门禁留库不算采集失败）
        if is_collect_fail_reason(str(e)):
            _log_collect_fail(str(e), result.get("source_id"))
        return result


def run_available_batches(
    *,
    client: MiaoshouClient,
    feishu: FeishuBitable,
    fcfg: dict,
    scheme: dict,
    publish_cfg: dict,
    blank_dir: Path,
    fill_urls: list[str],
    quota: DailyQuota,
    out_dir: Path,
    log: LogFn | None = None,
) -> dict[str, Any]:
    _log = log or print
    batch_size = int(fcfg.get("batch_size") or 200)
    split_cfg = fcfg.get("split") or []
    summary: dict[str, Any] = {"batches": [], "processed": 0, "ok": 0, "fail": 0, "stopped_reason": ""}

    while True:
        records = feishu.list_records(page_size=200)
        pending = pending_rows(records, fcfg)
        _log(f"待处理 {len(pending)} / 阈值={quota.snapshot([int(s['shop_id']) for s in split_cfg])}")
        if len(pending) < batch_size:
            summary["stopped_reason"] = f"不足{batch_size}条"
            break
        remain_total = sum(quota.remaining(int(s["shop_id"])) for s in split_cfg)
        if remain_total <= 0:
            summary["stopped_reason"] = "两店今日均已达日限额"
            break

        assigned = split_batch(pending, split_cfg, quota, batch_size)
        if not assigned:
            summary["stopped_reason"] = "今日配额不足，无法组批"
            break

        _log(f"本批 {len(assigned)} 条开始…")
        batch_results = []
        for row in assigned:
            _log(f"→ {row['shop_name']} {row['link'][:70]}")
            one = process_one_row(
                client,
                feishu,
                row,
                fcfg=fcfg,
                scheme=scheme,
                publish_cfg=publish_cfg,
                blank_dir=blank_dir,
                fill_urls=fill_urls,
                quota=quota,
                log=_log,
            )
            batch_results.append(one)
            summary["processed"] += 1
            if one.get("ok"):
                summary["ok"] += 1
            else:
                summary["fail"] += 1
            time.sleep(1.2)

        summary["batches"].append(
            {
                "size": len(assigned),
                "ok": sum(1 for r in batch_results if r.get("ok")),
                "fail": sum(1 for r in batch_results if not r.get("ok")),
            }
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"auto_batch_{stamp}.json").write_text(
            json.dumps(batch_results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        # continue until < batch_size or quota exhausted
        if sum(quota.remaining(int(s["shop_id"])) for s in split_cfg) <= 0:
            summary["stopped_reason"] = "两店今日均已达日限额"
            break

    return summary
