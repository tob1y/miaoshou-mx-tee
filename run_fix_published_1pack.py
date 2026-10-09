"""已上架（及店铺箱未发）多件装 → 标题去掉 N 件装 + 每包数量=1，已上架的再同步发布。"""
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
from rules.template_std import upsert_pack_quantity_one
from rules.titles import is_multipack_text, strip_multipack_text
from services.publisher import publish_details
from services.transformer import sanitize_size_chart, sanitize_video_fields

SHOPS = {18545217: "小韩1店", 18545044: "小赵1店"}


def shop_id_of(item: dict) -> int | None:
    shops = item.get("collectBoxDetailShopList") or []
    for s in shops:
        if isinstance(s, dict) and s.get("shopId") is not None:
            sid = int(s["shopId"])
            if sid in SHOPS:
                return sid
    return None


def list_status(client: MiaoshouClient, status: str) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 50):
        body = client.search_tiktok_box(page_no=page, page_size=100, status=status)
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        time.sleep(0.25)
    return out


def pack_attr_not_one(product: dict) -> bool:
    for a in product.get("productAttributes") or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("attributeId") or "") != "100347":
            continue
        vals = a.get("attributeValues") or []
        names = [str(v.get("valueName") or v.get("name") or "") for v in vals if isinstance(v, dict)]
        return any(n.strip() not in ("", "1") for n in names)
    return False


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / "fix_published_1pack.log"

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
        min_interval=1.2,
        max_retries=3,
    )
    pub_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    anti = dict(pub_cfg.get("anti_duplicate") or {})
    anti["filter_published"] = False
    pub_cfg["anti_duplicate"] = anti

    targets: list[dict] = []
    seen: set[int] = set()
    for status in ("published", "notPublished"):
        rows = list_status(client, status)
        log(f"扫描 {status}: {len(rows)} 条")
        for it in rows:
            title = str(it.get("title") or "")
            if not is_multipack_text(title):
                continue
            try:
                did = int(it["collectBoxDetailId"])
            except Exception:
                continue
            if did in seen:
                continue
            sid = shop_id_of(it)
            if sid is None:
                continue
            seen.add(did)
            targets.append(
                {
                    "detail_id": did,
                    "shop_id": sid,
                    "shop_name": SHOPS[sid],
                    "status": status,
                    "title": title,
                    "item_num": it.get("itemNum"),
                }
            )

    log(f"标题命中多件装 {len(targets)} 条")
    results = []
    ok = fail = 0
    result_path = out_dir / f"fix_published_1pack_{stamp}.json"

    for i, row in enumerate(targets, 1):
        t0 = time.time()
        did = row["detail_id"]
        sid = row["shop_id"]
        log(f"[{i}/{len(targets)}] {row['status']} {row['shop_name']} detail={did} {row['title'][:80]}")
        entry = dict(row)
        entry["ok"] = False
        try:
            client.claim_to_shops([did], [sid])
            time.sleep(0.8)
            oss, product = client.get_shop_detail(did, sid)
            before_title = str(product.get("title") or "")
            after_title = strip_multipack_text(before_title)
            if after_title and after_title[0].islower():
                after_title = after_title[0].upper() + after_title[1:]
            if not after_title:
                after_title = "Playera estampada casual unisex"
            product["title"] = after_title
            if product.get("oriTitle"):
                product["oriTitle"] = strip_multipack_text(str(product.get("oriTitle") or "")) or product["oriTitle"]
            upsert_pack_quantity_one(product)
            sanitize_size_chart(product)
            sanitize_video_fields(product)
            save = client.save_shop_detail(oss, product, did, sid)
            entry["before_title"] = before_title
            entry["after_title"] = after_title
            entry["save_code"] = save.get("code") if isinstance(save, dict) else None
            if row["status"] == "published":
                cfg = dict(pub_cfg)
                cfg["shop_id"] = sid
                cfg["shop_name"] = row["shop_name"]
                pub = publish_details(
                    client,
                    [did],
                    cfg,
                    dry_run=False,
                    confirm=True,
                    prep_before_publish=False,
                )
                calls = pub.get("publish_calls") or []
                entry["publish_ok"] = bool(calls) and all(c.get("ok") for c in calls)
                if not entry["publish_ok"]:
                    raise RuntimeError(f"同步发布失败: {calls}")
            else:
                entry["publish_ok"] = False
                entry["note"] = "未发布箱只改品，不重新上架"
            entry["ok"] = True
            ok += 1
            log(f"  OK {time.time()-t0:.1f}s → {after_title[:80]}")
        except Exception as e:
            fail += 1
            entry["error"] = str(e)
            log(f"  FAIL {time.time()-t0:.1f}s {e}")
        results.append(entry)
        result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        time.sleep(0.8)

    summary = {"total": len(targets), "ok": ok, "fail": fail}
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"fix_published_1pack_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
