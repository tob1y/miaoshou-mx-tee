"""两店已发布：有长袖的改掉（删长袖SKU）或纯长袖下架（删采集箱明细）。

说明：妙手 OpenAPI 无独立「TikTok 前台下架」路由；
纯长袖用 delete_collect_box_detail（采集箱删除，published 列表会消失）。
混装则 filter_sleeves + 改标题 + 保存 + 再发布同步。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient
from rules.sleeves import DEFAULT_DELETE, DEFAULT_KEEP, _hit, _label_of, filter_sleeves
from rules.titles import build_title
from services.publisher import publish_details
from services.transformer import sanitize_size_chart, sanitize_sku_map, sanitize_video_fields

SHOPS = {18545217: "小韩1店", 18545044: "小赵1店"}
DELETE_PATH = "/open/v1/product/collect_box/tiktok/collect_box/delete_collect_box_detail"
TITLE_LONG = re.compile(
    r"(?i)\b(manga\s+larga|long[\s\-]?sleeve|longsleeve|full[\s\-]?sleeve|长袖|長袖)\b"
)


def list_published(client: MiaoshouClient) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 50):
        body = client.search_tiktok_box(page_no=page, page_size=100, status="published")
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            shops = it.get("collectBoxDetailShopList") or []
            sids = [
                int(x["shopId"])
                for x in shops
                if isinstance(x, dict) and x.get("shopId") is not None
            ]
            our = [sid for sid in sids if sid in SHOPS]
            if not our:
                continue
            out.append(
                {
                    "detail_id": int(it["collectBoxDetailId"]),
                    "shop_id": our[0],
                    "shop_name": SHOPS[our[0]],
                    "title": str(it.get("title") or ""),
                    "item_num": it.get("itemNum"),
                }
            )
        if len(batch) < 100:
            break
        time.sleep(0.25)
    return out


def classify_product(product: dict, list_title: str) -> dict:
    title = str(product.get("title") or list_title or "")
    title_long = bool(TITLE_LONG.search(title))
    long_labels: list[str] = []
    short_labels: list[str] = []
    for prop in product.get("skuPropertyList") or []:
        if not isinstance(prop, dict):
            continue
        for v in prop.get("attrValueList") or []:
            lb = _label_of(v)
            if _hit(lb, DEFAULT_DELETE):
                long_labels.append(lb)
            if _hit(lb, DEFAULT_KEEP):
                short_labels.append(lb)
    # dry-run filter to see mode
    probe = json.loads(json.dumps(product))
    scheme = {"delete_sleeves": True}
    rep = filter_sleeves(probe, scheme)
    return {
        "title": title[:160],
        "title_long": title_long,
        "long_labels": sorted(set(long_labels)),
        "short_labels": sorted(set(short_labels)),
        "sleeve_filter_mode": rep.get("mode"),
        "sleeve_deleted": rep.get("deleted") or [],
        "sleeve_kept": rep.get("kept") or [],
    }


def strip_long_from_title(title: str) -> str:
    t = TITLE_LONG.sub("", title or "")
    t = re.sub(r"\s*,\s*,+", ", ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-()")
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    # 再用标准标题清洗（不去掉非长袖内容）
    return build_title(t, [], {"title_max_chars": 200, "title_min_chars": 25})


def delete_collect_detail(client: MiaoshouClient, detail_id: int, shop_id: int) -> dict:
    return client.assert_success(
        client.post(
            DELETE_PATH,
            {"detailIds": [int(detail_id)], "shopIds": [int(shop_id)]},
        ),
        "删除采集箱明细",
    )


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / "fix_longsleeve_takedown.log"
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    pub_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    anti = dict(pub_cfg.get("anti_duplicate") or {})
    anti["filter_published"] = False
    pub_cfg["anti_duplicate"] = anti

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 90),
        min_interval=1.1,
        max_retries=3,
    )

    rows = list_published(client)
    log(f"已发布扫描 {len(rows)} 条（两店）")

    # Pass 1: classify — title hits always inspect; also inspect all for SKU long sleeve
    # To finish in reasonable time: inspect ALL (user asked thoroughly)
    targets: list[dict] = []
    for i, row in enumerate(rows, 1):
        if i % 50 == 0:
            log(f"  分类进度 {i}/{len(rows)}")
        try:
            client.claim_to_shops([row["detail_id"]], [row["shop_id"]])
            time.sleep(0.35)
            oss, product = client.get_shop_detail(row["detail_id"], row["shop_id"])
        except Exception as e:
            # 已删或读不到
            if "已被删除" in str(e):
                continue
            log(f"  读详情失败 {row['detail_id']}: {e}")
            continue
        info = classify_product(product, row["title"])
        need = bool(
            info["title_long"]
            or info["long_labels"]
            or info["sleeve_filter_mode"] == "delete_long_sleeve"
            or info["sleeve_filter_mode"] == "abort_would_empty"
        )
        if not need:
            continue
        # pure long: title long and no short labels, OR abort_would_empty
        pure = (
            info["sleeve_filter_mode"] == "abort_would_empty"
            or (info["title_long"] and not info["short_labels"] and not info["long_labels"])
            or (bool(info["long_labels"]) and not info["short_labels"] and info["sleeve_filter_mode"] != "delete_long_sleeve")
        )
        # mixed editable
        mixed = info["sleeve_filter_mode"] == "delete_long_sleeve"
        action = "delete" if pure and not mixed else ("edit" if mixed else "delete")
        # title-only long with no sleeve axis → delete (whole product is long sleeve)
        if info["title_long"] and info["sleeve_filter_mode"] in ("no_sleeve_axis", "nothing_to_delete", "abort_would_empty"):
            action = "delete"
        targets.append({**row, **info, "action": action, "oss": None})
        # don't keep oss in list file
        log(
            f"命中 {row['shop_name']} {row['detail_id']} action={action} "
            f"title_long={info['title_long']} long={info['long_labels']} short={info['short_labels']} mode={info['sleeve_filter_mode']}"
        )

    plan_path = out_dir / f"fix_longsleeve_plan_{stamp}.json"
    plan_path.write_text(
        json.dumps([{k: v for k, v in t.items() if k != "oss"} for t in targets], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"命中 {len(targets)} 条 → 计划 {plan_path.name}")

    results = []
    ok = fail = 0
    result_path = out_dir / f"fix_longsleeve_takedown_{stamp}.json"

    for i, t in enumerate(targets, 1):
        did = t["detail_id"]
        sid = t["shop_id"]
        action = t["action"]
        log(f"[{i}/{len(targets)}] {action} {t['shop_name']} detail={did}")
        entry = {k: v for k, v in t.items() if k != "oss"}
        entry["ok"] = False
        try:
            if action == "delete":
                delete_collect_detail(client, did, sid)
                entry["ok"] = True
                entry["result"] = "deleted_collect_box"
                ok += 1
                log("  OK deleted collect box detail")
            else:
                client.claim_to_shops([did], [sid])
                time.sleep(0.6)
                oss, product = client.get_shop_detail(did, sid)
                before = str(product.get("title") or "")
                sleeve_rep = filter_sleeves(product, scheme)
                if sleeve_rep.get("mode") != "delete_long_sleeve":
                    # fallback delete if can't safely edit
                    delete_collect_detail(client, did, sid)
                    entry["ok"] = True
                    entry["result"] = "deleted_fallback"
                    entry["sleeve"] = sleeve_rep
                    ok += 1
                    log(f"  OK fallback delete (sleeve mode={sleeve_rep.get('mode')})")
                else:
                    product["title"] = strip_long_from_title(before)
                    if product.get("oriTitle"):
                        product["oriTitle"] = strip_long_from_title(str(product.get("oriTitle")))
                    sanitize_sku_map(product)
                    sanitize_size_chart(product)
                    sanitize_video_fields(product)
                    save = client.save_shop_detail(oss, product, did, sid)
                    cfg = dict(pub_cfg)
                    cfg["shop_id"] = sid
                    cfg["shop_name"] = t["shop_name"]
                    pub = publish_details(
                        client,
                        [did],
                        cfg,
                        dry_run=False,
                        confirm=True,
                        prep_before_publish=False,
                    )
                    calls = pub.get("publish_calls") or []
                    pub_ok = bool(calls) and all(c.get("ok") for c in calls)
                    if not pub_ok:
                        raise RuntimeError(f"publish failed: {calls}")
                    entry["ok"] = True
                    entry["result"] = "edited_removed_long_sleeve"
                    entry["before_title"] = before
                    entry["after_title"] = product.get("title")
                    entry["sleeve"] = sleeve_rep
                    entry["save_code"] = save.get("code") if isinstance(save, dict) else None
                    ok += 1
                    log(f"  OK edited → {(product.get('title') or '')[:70]}")
        except Exception as e:
            fail += 1
            entry["error"] = str(e)
            log(f"  FAIL {e}")
        results.append(entry)
        result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        time.sleep(0.6)

    summary = {"total": len(targets), "ok": ok, "fail": fail}
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"fix_longsleeve_takedown_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
