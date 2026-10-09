"""Retry failed items from collect_split_results_*.json."""
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
from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.auto_batch import edit_and_publish_one
from services.daily_quota import DailyQuota
from services.full_pipeline import claim_public_to_shop, qps_retry, wait_shop_detail


def log(msg: str, path: Path) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_detail_by_source(client: MiaoshouClient, source_id: str) -> int | None:
    if not source_id:
        return None
    body = client.search_tiktok_box(page_no=1, page_size=50, status=None, source_item_id=source_id)
    for it in (body.get("data") or {}).get("detailList") or []:
        try:
            return int(it.get("collectBoxDetailId"))
        except Exception:
            continue
    return None


def find_detail_by_common(client: MiaoshouClient, common_id: int, max_pages: int = 15) -> int | None:
    for page in range(1, max_pages + 1):
        body = client.search_tiktok_box(page_no=page, page_size=100, status=None)
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            try:
                if int(it.get("commonCollectBoxDetailId") or 0) == int(common_id):
                    return int(it["collectBoxDetailId"])
            except Exception:
                continue
        if len(batch) < 100:
            break
        time.sleep(0.8)
    return None


def ensure_detail(
    client: MiaoshouClient,
    *,
    common_id: int,
    source_id: str,
    shop_id: int,
    public_item: dict | None,
    log_fn,
) -> int:
    # 1) already in tiktok box?
    did = find_detail_by_source(client, source_id)
    if did:
        qps_retry(lambda: client.claim_to_shops([did], [shop_id]), "claim_shop", log=log_fn)
        return did
    did = find_detail_by_common(client, common_id)
    if did:
        qps_retry(lambda: client.claim_to_shops([did], [shop_id]), "claim_shop", log=log_fn)
        return did

    # 2) claim from public
    item = public_item or {"commonCollectBoxDetailId": common_id, "itemNum": source_id}
    mapping = {}
    try:
        mapping = claim_public_to_shop(client, [item], shop_id, log=log_fn)
    except Exception as e:
        log_fn(f"claim_public warn: {e}")
    did = mapping.get(int(common_id))
    if did:
        qps_retry(lambda: client.claim_to_shops([did], [shop_id]), "claim_shop", log=log_fn)
        return int(did)

    time.sleep(4)
    did = find_detail_by_source(client, source_id) or find_detail_by_common(client, common_id)
    if did:
        qps_retry(lambda: client.claim_to_shops([did], [shop_id]), "claim_shop", log=log_fn)
        return did
    raise MiaoshouError(f"仍无法获得 detailId common={common_id} source={source_id}")


def load_public_map(client: MiaoshouClient, log_fn) -> dict[int, dict]:
    out: dict[int, dict] = {}
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
                "public",
                log=log_fn,
            )
        except Exception as e:
            log_fn(f"public page {page}: {e}")
            time.sleep(5)
            continue
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            try:
                out[int(it["commonCollectBoxDetailId"])] = it
            except Exception:
                pass
        if len(batch) < 50:
            break
        page += 1
        time.sleep(1.0)
    return out


def main() -> int:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / "collect_split_retry.log"

    def _log(msg: str) -> None:
        log(msg, log_path)

    results_files = sorted(out_dir.glob("collect_split_results_*.json"))
    if not results_files:
        _log("无上次结果文件")
        return 1
    prev = json.loads(results_files[-1].read_text(encoding="utf-8"))
    fails = [r for r in prev if not r.get("ok")]
    _log(f"重试失败 {len(fails)} 条（来自 {results_files[-1].name}）")

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=2.0,
        max_retries=4,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    quota = DailyQuota(ROOT / "data" / "daily_quota.json", limit_per_shop=300)

    public_map = load_public_map(client, _log)
    _log(f"公共箱索引 {len(public_map)}")

    results = []
    ok = fail = 0
    for i, row in enumerate(fails, 1):
        common_id = int(row["common_id"])
        shop_id = int(row["shop_id"])
        shop_name = row["shop_name"]
        source_id = str(row.get("item_num") or "")
        pub = public_map.get(common_id)
        if pub and not source_id:
            source_id = str(pub.get("itemNum") or "")
            for src in pub.get("sourceList") or []:
                if isinstance(src, dict) and src.get("sourceItemId"):
                    source_id = str(src["sourceItemId"])
                    break
        _log(f"[{i}/{len(fails)}] retry → {shop_name} common={common_id} source={source_id}")
        entry = {
            "common_id": common_id,
            "item_num": source_id,
            "shop_id": shop_id,
            "shop_name": shop_name,
            "ok": False,
            "retry_of": row.get("error"),
        }
        try:
            detail_id = ensure_detail(
                client,
                common_id=common_id,
                source_id=source_id,
                shop_id=shop_id,
                public_item=pub,
                log_fn=_log,
            )
            entry["detail_id"] = detail_id
            # 并发冲突：重新拉详情再存
            time.sleep(2.0)
            wait_shop_detail(client, detail_id, shop_id, log=_log)
            time.sleep(1.0)
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
            entry.update(ep_res)
            if ep_res.get("ok"):
                ok += 1
                quota.add(shop_id, 1)
                _log(f"  OK detail={detail_id}")
            else:
                fail += 1
                _log(f"  PUB_FAIL {ep_res.get('publish')}")
        except Exception as e:
            fail += 1
            entry["error"] = str(e)
            _log(f"  FAIL: {e}")
            _log(traceback.format_exc()[-400:])
        results.append(entry)
        (out_dir / f"collect_split_retry_{stamp}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        time.sleep(2.0)

    summary = {"retry_total": len(fails), "ok": ok, "fail": fail, "quota": quota.snapshot([18545217, 18545044])}
    _log(f"=== 重试结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"collect_split_retry_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
