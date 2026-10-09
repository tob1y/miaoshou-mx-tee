"""公共采集箱未上架商品 → 小韩/小赵对半认领 → 改品 → 上架。

说明：TikTok 采集箱里 currently 全部已是 published；本脚本处理公共箱里
尚未出现在已上架列表中的 success 商品。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one
from services.daily_quota import DailyQuota
from services.full_pipeline import qps_retry

SHOPS = [
    {"shop_id": 18545217, "shop_name": "小韩1店"},
    {"shop_id": 18545044, "shop_name": "小赵1店"},
]


def log(msg: str, path: Path) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def list_published_item_nums(client: MiaoshouClient) -> set[str]:
    out: set[str] = set()
    for page in range(1, 60):
        body = client.search_tiktok_box(page_no=page, page_size=100, status="published")
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            if it.get("itemNum"):
                out.add(str(it["itemNum"]))
        if len(batch) < 100:
            break
        time.sleep(0.8)
    return out


def list_public_success(client: MiaoshouClient, log_fn) -> list[dict]:
    items: list[dict] = []
    page = 1
    while page <= 60:
        try:
            body = qps_retry(
                lambda p=page: client.assert_success(
                    client.post(
                        ep.PUBLIC_LIST,
                        {"pageNo": p, "pageSize": 50, "filter": {"status": "success"}},
                    ),
                    "公共箱",
                ),
                "public_list",
                log=log_fn,
            )
        except Exception as e:
            log_fn(f"public page {page} err, retry: {e}")
            time.sleep(6)
            body = client.assert_success(
                client.post(
                    ep.PUBLIC_LIST,
                    {"pageNo": page, "pageSize": 50, "filter": {"status": "success"}},
                ),
                "公共箱",
            )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 50:
            break
        page += 1
        time.sleep(1.2)
    return items


def source_ids(it: dict) -> list[str]:
    sids: list[str] = []
    if it.get("itemNum"):
        sids.append(str(it["itemNum"]))
    for src in it.get("sourceList") or []:
        if isinstance(src, dict) and src.get("sourceItemId"):
            sids.append(str(src["sourceItemId"]))
    return sids


def split_half(items: list[dict], quota: DailyQuota) -> list[dict]:
    """Assign alternately / half-half, capped by daily remaining."""
    remain = {s["shop_id"]: quota.remaining(s["shop_id"]) for s in SHOPS}
    capacity = sum(remain.values())
    take = items[:capacity]
    n = len(take)
    a_n = min(remain[SHOPS[0]["shop_id"]], n // 2 + n % 2)
    b_n = min(remain[SHOPS[1]["shop_id"]], n - a_n)
    if a_n + b_n < n:
        leftover = n - a_n - b_n
        for shop, key in ((SHOPS[0], "a"), (SHOPS[1], "b")):
            if leftover <= 0:
                break
            sid = shop["shop_id"]
            cur = a_n if key == "a" else b_n
            extra = min(leftover, remain[sid] - cur)
            if key == "a":
                a_n += extra
            else:
                b_n += extra
            leftover -= extra

    assigned: list[dict] = []
    idx = 0
    for _ in range(a_n):
        assigned.append({**take[idx], **SHOPS[0]})
        idx += 1
    for _ in range(b_n):
        assigned.append({**take[idx], **SHOPS[1]})
        idx += 1
    return assigned


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "collect_split_publish.log"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _log(msg: str) -> None:
        log(msg, log_path)

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=float(m.get("min_request_interval_seconds") or 1.5),
        max_retries=3,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    fcfg = settings.get("feishu") or {}
    quota = DailyQuota(
        ROOT / "data" / "daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )

    _log("=== 开始：公共箱未上架 → 对半改品上架 ===")
    _log(f"quota={quota.snapshot([s['shop_id'] for s in SHOPS])}")

    pub_nums = list_published_item_nums(client)
    _log(f"已上架 itemNum 数={len(pub_nums)}")
    public = list_public_success(client, _log)
    _log(f"公共箱 success={len(public)}")

    fresh = []
    for it in public:
        sids = source_ids(it)
        if any(x in pub_nums for x in sids):
            continue
        fresh.append(it)
    _log(f"未上架候选={len(fresh)}")
    if not fresh:
        _log("没有可处理商品，退出")
        return 0

    assigned = split_half(fresh, quota)
    han = sum(1 for x in assigned if x["shop_id"] == 18545217)
    zhao = sum(1 for x in assigned if x["shop_id"] == 18545044)
    _log(f"本批分配 小韩={han} 小赵={zhao} 合计={len(assigned)}")
    (out_dir / f"collect_split_plan_{stamp}.json").write_text(
        json.dumps(
            [
                {
                    "common_id": it.get("commonCollectBoxDetailId"),
                    "item_num": it.get("itemNum"),
                    "title": it.get("title"),
                    "shop_id": it["shop_id"],
                    "shop_name": it["shop_name"],
                }
                for it in assigned
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    results = []
    ok = fail = 0
    for i, it in enumerate(assigned, 1):
        common_id = int(it["commonCollectBoxDetailId"])
        shop_id = int(it["shop_id"])
        shop_name = it["shop_name"]
        title = str(it.get("title") or "")[:50]
        source_id = str(it.get("itemNum") or (source_ids(it)[0] if source_ids(it) else ""))
        _log(f"[{i}/{len(assigned)}] → {shop_name} common={common_id} {title}")
        row = {
            "common_id": common_id,
            "item_num": it.get("itemNum"),
            "shop_id": shop_id,
            "shop_name": shop_name,
            "title": it.get("title"),
            "ok": False,
        }
        try:
            detail_id = claim_to_detail(client, it, shop_id, source_id or str(common_id), _log)
            row["detail_id"] = detail_id
            ep_res = edit_and_publish_one(
                client,
                detail_id=detail_id,
                shop_id=shop_id,
                shop_name=shop_name,
                scheme=scheme,
                publish_cfg=publish_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                log=_log,
            )
            row.update(ep_res)
            if ep_res.get("ok"):
                ok += 1
                quota.add(shop_id, 1)
                _log(f"  OK detail={detail_id}")
            else:
                fail += 1
                _log(f"  PUB_FAIL detail={detail_id} {ep_res.get('publish')}")
        except Exception as e:
            fail += 1
            row["error"] = str(e)
            _log(f"  FAIL: {e}")
            _log(traceback.format_exc()[-500:])
        results.append(row)
        # checkpoint every item
        (out_dir / f"collect_split_results_{stamp}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(1.5)

    summary = {
        "total": len(assigned),
        "ok": ok,
        "fail": fail,
        "quota_after": quota.snapshot([s["shop_id"] for s in SHOPS]),
        "han": han,
        "zhao": zhao,
    }
    _log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"collect_split_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
