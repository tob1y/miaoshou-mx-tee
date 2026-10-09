"""Claim 小赵 public-box → 小赵1店, then edit+mark 2 items for review."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient, MiaoshouError
from services.transformer import transform_product

SHOP_ID = 18545044  # 小赵1店
GROUP_NAME = "小赵"
MARK = "【测试改品-小赵】"


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}


def list_public(client: MiaoshouClient) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        for attempt in range(1, 6):
            try:
                body = client.assert_success(
                    client.post(ep.PUBLIC_LIST, {"pageNo": page, "pageSize": 100}),
                    "公共采集箱",
                )
                break
            except MiaoshouError as e:
                if "Qps" in str(e) or "Qpm" in str(e) or "频率" in str(e):
                    time.sleep(2.5 * attempt)
                    continue
                raise
        else:
            raise MiaoshouError("公共采集箱列表限流重试失败")
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(1.5)
    return items


def claim_group(client: MiaoshouClient, zhao: list[dict]) -> dict[int, int]:
    detail_list = [
        {
            "detailId": int(it["commonCollectBoxDetailId"]),
            "platform": "tiktok",
            "serialNumber": i,
        }
        for i, it in enumerate(zhao, start=1)
    ]
    body = {
        "detailSerialNumberPlatformList": detail_list,
        "shopIds": [SHOP_ID],
    }
    for attempt in range(1, 6):
        try:
            resp = client.assert_success(client.post(ep.CLAIM_PUBLIC, body), "认领")
            break
        except MiaoshouError as e:
            if "Qps" in str(e) or "Qpm" in str(e) or "频率" in str(e):
                time.sleep(3 * attempt)
                continue
            raise
    else:
        raise MiaoshouError("认领限流重试失败")
    id_map = ((resp.get("data") or {}).get("platformCollectBoxDetailIdMap") or {}).get("tiktok") or {}
    return {int(k): int(v) for k, v in id_map.items()}


def wait_detail(client: MiaoshouClient, detail_id: int) -> tuple[str, dict]:
    last: Exception | None = None
    for attempt in range(1, 12):
        try:
            oss, product = client.get_shop_detail(detail_id, SHOP_ID)
            if product.get("title") or product.get("skuPropertyList") or product.get("skuMap"):
                return oss, product
        except MiaoshouError as e:
            last = e
            print(f"  等待详情 detailId={detail_id} attempt={attempt}: {e}")
            time.sleep(2.5)
            continue
        time.sleep(2.0)
    raise MiaoshouError(f"详情未就绪 detailId={detail_id}: {last}")


def main() -> int:
    settings = load_settings()
    m = settings.get("miaoshou") or {}
    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=1.5,
    )
    scheme = json.loads((ROOT / "config" / "schemes" / "default.json").read_text(encoding="utf-8"))
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    out_dir = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("拉取公共采集箱…")
    time.sleep(2)
    all_items = list_public(client)
    zhao = [it for it in all_items if (it.get("commonCollectBoxGroupName") or "") == GROUP_NAME]
    print(f"分组 {GROUP_NAME}: {len(zhao)} 条")
    if not zhao:
        print("没有小赵分组商品")
        return 1

    print("认领到小赵1店（公共箱→平台箱）…")
    mapping = claim_group(client, zhao)
    print(f"认领映射 {len(mapping)} 条")
    for k, v in list(mapping.items())[:3]:
        print(f"  common {k} -> detail {v}")

    print("认领到店铺采集箱 claim_to_shop…")
    detail_ids = list(mapping.values())
    for i in range(0, len(detail_ids), 10):
        chunk = detail_ids[i : i + 10]
        client.assert_success(
            client.post(ep.CLAIM_TO_SHOP, {"detailIds": chunk, "shopIds": [SHOP_ID]}),
            "认领店铺",
        )
        time.sleep(1.5)

    print("等待店铺采集箱生成…")
    time.sleep(6)

    picked: list[tuple[int, int, dict]] = []
    for it in zhao:
        cid = int(it["commonCollectBoxDetailId"])
        if cid in mapping:
            picked.append((cid, mapping[cid], it))
        if len(picked) >= 2:
            break
    print(f"本次改品 {len(picked)} 条（带标注 {MARK}）")

    results = []
    for common_id, detail_id, it in picked:
        print(f"\n==== 改品 detailId={detail_id} commonId={common_id} ====")
        try:
            oss, product = wait_detail(client, detail_id)
            original_title = product.get("title")
            report = transform_product(product, scheme, blank)
            new_title = str(report["product"].get("title") or "")
            if MARK not in new_title:
                marked = f"{MARK}{new_title}"
                max_len = int(scheme.get("title_max_chars") or 200)
                report["product"]["title"] = marked[:max_len]
                report["title"]["proposed"] = report["product"]["title"]
            report["product"]["remark"] = (
                f"{MARK} shop={SHOP_ID} detailId={detail_id} "
                f"ts={datetime.now().isoformat(timespec='seconds')}"
            )
            pending = (report["product"] or {}).pop("_pending_local_images", None) or []
            save_body = client.save_shop_detail(oss, report["product"], detail_id, SHOP_ID)
            time.sleep(2)
            _, saved = client.get_shop_detail(detail_id, SHOP_ID)
            entry = {
                "ok": True,
                "shop_id": SHOP_ID,
                "shop_name": "小赵1店",
                "common_collect_id": common_id,
                "detail_id": detail_id,
                "item_num": it.get("itemNum"),
                "mark": MARK,
                "original_title": original_title,
                "new_title": saved.get("title"),
                "remark": saved.get("remark"),
                "colors": report.get("colors"),
                "sizes": report.get("sizes"),
                "images_note": report.get("images"),
                "pending_local_images": len(pending),
                "save_code": save_body.get("code") if isinstance(save_body, dict) else None,
            }
            results.append(entry)
            print("原标题:", original_title)
            print("新标题:", saved.get("title"))
            print("颜色:", report.get("colors"))
            print("尺码:", report.get("sizes"))
        except Exception as e:
            results.append(
                {
                    "ok": False,
                    "common_collect_id": common_id,
                    "detail_id": detail_id,
                    "error": str(e),
                }
            )
            print("失败:", e)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"xiaozhao_claim_edit_2_{stamp}.json"
    payload = {
        "shop_id": SHOP_ID,
        "shop_name": "小赵1店",
        "group": GROUP_NAME,
        "claimed_count": len(mapping),
        "mapping": {str(k): v for k, v in mapping.items()},
        "edited": results,
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n报告:", report_path)
    ok_n = sum(1 for r in results if r.get("ok"))
    print(f"成功 {ok_n}/{len(results)}")
    return 0 if ok_n == len(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
