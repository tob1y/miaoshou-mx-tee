from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rules.colors import filter_colors, match_color
from rules.product_gates import (
    merge_hold_reports,
    should_hold_hoodie,
    should_hold_multipack,
    should_hold_random_assortment,
    should_hold_wrong_category,
    should_require_hoodie,
    sku_health_check,
)
from rules.publish_color_gate import should_hold_without_publish
from rules.vision_gate import apply_vision_delete_only
from rules.sleeves import filter_sleeves
from rules.sizes import plan_and_apply_sizes
from rules.titles import apply_title
from rules.template_std import (
    apply_brand,
    apply_category,
    apply_detail_images,
    apply_product_attributes,
)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def transform_product(
    product: dict[str, Any],
    scheme: dict[str, Any],
    blank_tee_dir: Path,
    fill_image_urls: list[str] | None = None,
    vision_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """In-memory transform. Returns report. Mutates product.

    流水线：
    1) 硬文字门禁（卫衣/多件装/童装/随机）→ 整链留库，不改品
    2) 识图只删不补：品类/袖/衣身色；库外色删主图与选项；不填充黑白；看不清留库
       （原「标题+选项无颜色」不再在识图前一刀切，交给识图补色名或留库）
    3) 过关后再删长袖漏网、库外色文字清理，并做格式/价钱系统化
    """
    out = json.loads(json.dumps(product))  # work on copy then caller can replace
    report: dict[str, Any] = {}

    original_title = str(product.get("oriTitle") or product.get("title") or out.get("title") or "")

    # —— 1) 硬文字门禁（不含「无颜色」；无颜色走识图只删不补）——
    cat_hold = should_hold_wrong_category(out, scheme, original_title=original_title)
    cat_hold["gate"] = "wrong_category"
    mp_hold = should_hold_multipack(out, scheme, original_title=original_title)
    mp_hold["gate"] = "multipack"
    rand_hold = should_hold_random_assortment(out, scheme, original_title=original_title)
    rand_hold["gate"] = "random_assortment"
    color_hold = should_hold_without_publish(out, scheme, original_title=original_title)
    color_hold["gate"] = "no_color"
    hoodie_hold = should_hold_hoodie(out, scheme, original_title=original_title)
    require_hoodie_hold = should_require_hoodie(out, scheme, original_title=original_title)

    vision_hold: dict[str, Any] = {
        "gate": "vision_delete_only",
        "enabled": False,
        "hold": False,
        "reason": "skipped_pending_text_gates",
        "mutate": False,
    }
    sleeve_hold: dict[str, Any] = {
        "gate": "long_sleeve",
        "hold": False,
        "reason": "skipped_pending_gates",
        "mode": "",
    }
    sku_hold: dict[str, Any] = {
        "gate": "sku_health",
        "hold": False,
        "reason": "skipped_pending_gates",
        "ok": True,
    }

    text_hard = merge_hold_reports(
        cat_hold, mp_hold, rand_hold, hoodie_hold, require_hoodie_hold
    )
    if text_hard.get("hold"):
        hold_report = merge_hold_reports(text_hard, color_hold)
        hold_report["title_colors"] = color_hold.get("title_colors")
        hold_report["options"] = color_hold.get("options")
        hold_report["title"] = original_title[:120]
        hold_report["vision"] = vision_hold
        report["sleeves"] = {"mode": "skipped_text_hold"}
        report["colors"] = {"mode": "skipped_text_hold"}
        report["hold_without_publish"] = hold_report
        report["product"] = out  # 原样，不改品
        return report

    # —— 2) 识图只删不补（含异色袖 / 非纯黑 / 库外色删留）——
    vision_hold = apply_vision_delete_only(out, vision_cfg, scheme)
    if vision_hold.get("hold"):
        hold_report = merge_hold_reports(
            cat_hold,
            mp_hold,
            rand_hold,
            color_hold,
            hoodie_hold,
            require_hoodie_hold,
            vision_hold,
        )
        hold_report["title_colors"] = color_hold.get("title_colors")
        hold_report["options"] = color_hold.get("options")
        hold_report["title"] = original_title[:120]
        hold_report["vision"] = vision_hold
        report["sleeves"] = {"mode": "skipped_vision_hold"}
        report["colors"] = {"mode": "skipped_vision_hold", "vision": vision_hold}
        report["hold_without_publish"] = hold_report
        report["product"] = out
        return report

    # 识图写回色名后再查一次「是否有可上架颜色信号」
    color_hold = should_hold_without_publish(out, scheme, original_title=original_title)
    color_hold["gate"] = "no_color"
    if color_hold.get("hold"):
        hold_report = merge_hold_reports(
            cat_hold,
            mp_hold,
            rand_hold,
            color_hold,
            hoodie_hold,
            require_hoodie_hold,
            vision_hold,
        )
        hold_report["title_colors"] = color_hold.get("title_colors")
        hold_report["options"] = color_hold.get("options")
        hold_report["title"] = original_title[:120]
        hold_report["vision"] = vision_hold
        report["sleeves"] = {"mode": "skipped_still_no_color"}
        report["colors"] = {"mode": "still_no_color_after_vision", "vision": vision_hold}
        report["hold_without_publish"] = hold_report
        report["product"] = out
        return report

    # —— 3) 过关：删长袖漏网 / 库外色文字清理，再系统化 ——
    sleeve_report = filter_sleeves(out, scheme)
    report["sleeves"] = sleeve_report
    sleeve_hold = {
        "gate": "long_sleeve",
        "hold": False,
        "reason": "ok",
        "mode": sleeve_report.get("mode"),
    }
    if sleeve_report.get("mode") == "abort_would_empty":
        sleeve_hold["hold"] = True
        sleeve_hold["reason"] = "long_sleeve_only"

    color_report = filter_colors(out, scheme)
    color_report["vision_mapped"] = vision_hold.get("mapped_colors")
    color_report["vision_actions"] = vision_hold.get("actions")
    report["colors"] = color_report

    size_report = plan_and_apply_sizes(out, scheme)
    report["sizes"] = size_report

    retained_keys = []
    for label in color_report.get("kept") or []:
        k = match_color(label)
        if k and k not in retained_keys:
            retained_keys.append(k)
    if color_report.get("mode") == "skip_style_codes":
        retained_keys = []
    # 识图映射色优先写入标题色信号
    for es in vision_hold.get("mapped_colors") or []:
        k = match_color(str(es))
        if k and k not in retained_keys:
            retained_keys.append(k)

    new_title = apply_title(out, scheme, retained_keys)
    report["title"] = {"original": product.get("title"), "proposed": new_title}

    report["brand"] = apply_brand(out, scheme)
    report["category"] = apply_category(out, scheme)
    report["product_attributes"] = apply_product_attributes(out, scheme)
    report["detail_images"] = apply_detail_images(out, scheme)

    rules = scheme.get("rules") or {}
    price = (rules.get("price") or {}).get("value") if (rules.get("price") or {}).get("enabled") else None
    stock = (rules.get("stock") or {}).get("value") if (rules.get("stock") or {}).get("enabled") else None
    weight_g = scheme.get("weight_g")
    dims = scheme.get("dimensions") or {}
    sku_map = out.get("skuMap") or {}
    priced = 0
    if isinstance(sku_map, dict):
        for sku in sku_map.values():
            if not isinstance(sku, dict):
                continue
            if str(sku.get("isDelete") or "0") in ("1", "true", "True"):
                continue
            if price is not None:
                sku["price"] = float(price)
                priced += 1
            if stock is not None:
                sku["stock"] = int(stock)
                wh_map = sku.get("shopIdToWarehouseIdAndStockMap")
                if isinstance(wh_map, dict):
                    for _sid, warehouses in wh_map.items():
                        if isinstance(warehouses, dict):
                            for wid in list(warehouses.keys()):
                                warehouses[wid] = str(int(stock))
    report["price"] = {"enabled": price is not None, "value": price, "sku_count": priced}
    report["price_template"] = scheme.get("price_template_name")
    if weight_g is not None:
        out["weight"] = round(float(weight_g) / 1000.0, 3)
    for src, dst in (
        ("length", "packageLength"),
        ("width", "packageWidth"),
        ("height", "packageHeight"),
    ):
        if dims.get(src) is not None:
            out[dst] = dims[src]

    url_pool = list(fill_image_urls or [])
    if not url_pool:
        url_pool = [str(u) for u in (scheme.get("fill_image_urls") or []) if str(u).startswith("http")]
    report["images"] = fill_main_images(
        out,
        blank_tee_dir,
        int(scheme.get("main_image_target") or 9),
        fill_image_urls=url_pool,
    )
    report["sku_sanitize"] = sanitize_sku_map(out)
    report["sku_cap"] = cap_active_skus(out, max_skus=int(scheme.get("max_skus") or 100))
    report["size_chart"] = sanitize_size_chart(out)
    report["video"] = sanitize_video_fields(out)

    sku_hold = sku_health_check(out)
    sku_hold["gate"] = "sku_health"
    if scheme.get("hold_bad_sku", True) is False:
        sku_hold["hold"] = False
        sku_hold["enabled"] = False
        sku_hold["reason"] = "disabled"

    hold_report = merge_hold_reports(
        cat_hold,
        mp_hold,
        rand_hold,
        color_hold,
        hoodie_hold,
        require_hoodie_hold,
        vision_hold,
        sleeve_hold,
        sku_hold,
    )
    hold_report["title_colors"] = color_hold.get("title_colors")
    hold_report["options"] = color_hold.get("options")
    hold_report["title"] = original_title[:120]
    hold_report["vision"] = vision_hold
    report["hold_without_publish"] = hold_report
    report["product"] = out
    return report


def _active_sku_count(product: dict[str, Any]) -> int:
    sku_map = product.get("skuMap") or {}
    if not isinstance(sku_map, dict):
        return 0
    n = 0
    for sku in sku_map.values():
        if not isinstance(sku, dict):
            continue
        if str(sku.get("isDelete") or "0") in ("1", "true", "True"):
            continue
        n += 1
    return n


def cap_active_skus(product: dict[str, Any], max_skus: int = 100) -> dict[str, Any]:
    """若有效 SKU 仍超平台上限：优先删库外色轴取值，再只留允许色，最后截断 skuMap。"""
    before = _active_sku_count(product)
    note: dict[str, Any] = {"before": before, "max": max_skus, "actions": []}
    if before <= max_skus:
        note["after"] = before
        return note

    from rules.colors import match_color

    props = product.get("skuPropertyList") or []
    color_prop = None
    color_idx = None
    for i, prop in enumerate(props):
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("name") or prop.get("attrName") or "").lower()
        if "color" in name or "colour" in name or "颜色" in name:
            color_prop, color_idx = prop, i
            break
    if color_prop is None:
        note["after"] = before
        note["actions"].append("no_color_axis")
        return note

    preferred = ["black", "white", "red", "blue", "navy", "pink"]
    values = list(color_prop.get("attrValueList") or [])
    keep_vals = []
    drop_ids: set[str] = set()
    for v in values:
        if not isinstance(v, dict):
            continue
        key = match_color(str(v.get("attrValue") or ""))
        if key in preferred:
            keep_vals.append(v)
        else:
            vid = str(v.get("attrValueId") or "")
            if vid:
                drop_ids.add(vid)

    # 若允许色都没有，至少留第一个
    if not keep_vals and values:
        first = values[0] if isinstance(values[0], dict) else None
        if first:
            keep_vals = [first]
            drop_ids = {
                str(v.get("attrValueId") or "")
                for v in values
                if isinstance(v, dict) and v is not first and v.get("attrValueId")
            }

    color_prop = dict(color_prop)
    color_prop["attrValueList"] = keep_vals
    props[color_idx] = color_prop
    product["skuPropertyList"] = props
    note["actions"].append(f"keep_colors={[v.get('attrValue') for v in keep_vals]}")

    sku_map = product.get("skuMap") or {}
    if isinstance(sku_map, dict) and drop_ids:
        new_map = {}
        for key, sku in sku_map.items():
            parts = [p for p in str(key).split(";") if p]
            if any(p in drop_ids for p in parts):
                continue
            new_map[key] = sku
        product["skuMap"] = new_map

    # 仍超限则硬截断
    after_color = _active_sku_count(product)
    if after_color > max_skus:
        sku_map = product.get("skuMap") or {}
        if isinstance(sku_map, dict):
            kept = {}
            for key, sku in sku_map.items():
                if not isinstance(sku, dict):
                    continue
                if str(sku.get("isDelete") or "0") in ("1", "true", "True"):
                    continue
                kept[key] = sku
                if len(kept) >= max_skus:
                    break
            product["skuMap"] = kept
            note["actions"].append(f"hard_truncate_to_{max_skus}")

    note["after"] = _active_sku_count(product)
    return note


def sanitize_video_fields(product: dict[str, Any]) -> dict[str, Any]:
    """mainImgVideoUrl 最长 255；超长则清空，避免保存失败。"""
    note: dict[str, Any] = {}
    for key in ("mainImgVideoUrl", "mainImgAppVideoId", "mainImgPlatformVideoId"):
        val = product.get(key)
        if val is None:
            continue
        s = str(val)
        if len(s) > 255:
            note[key] = {"cleared": True, "len": len(s)}
            product[key] = ""
        else:
            note[key] = {"cleared": False, "len": len(s)}
    return note


def sanitize_size_chart(product: dict[str, Any]) -> dict[str, Any]:
    """尺码表 URL 必须是 jpg/jpeg/png/webp；空或截断则清空，避免保存失败。"""
    url = str(product.get("sizeChart") or "").strip()
    typ = str(product.get("sizeChartType") or "").strip()
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    ok_ext = path.endswith((".jpg", ".jpeg", ".png", ".webp"))
    if (typ == "image" or url) and (not url or not ok_ext):
        product["sizeChart"] = ""
        product["sizeChartType"] = ""
        return {"cleared": True, "reason": "empty_or_invalid_url", "was": url[:120]}
    return {"cleared": False, "url": url[:120]}


def sanitize_sku_map(product: dict[str, Any]) -> dict[str, Any]:
    """Drop skuMap rows that reference attrValueIds not present in skuPropertyList."""
    props = product.get("skuPropertyList") or []
    valid: set[str] = set()
    for prop in props:
        if not isinstance(prop, dict):
            continue
        for v in prop.get("attrValueList") or []:
            if isinstance(v, dict) and v.get("attrValueId") and not v.get("isDelete"):
                valid.add(str(v["attrValueId"]))
    sku_map = product.get("skuMap") or {}
    if not isinstance(sku_map, dict) or not valid:
        return {"kept": len(sku_map) if isinstance(sku_map, dict) else 0, "dropped": 0}
    kept: dict[str, Any] = {}
    dropped = 0
    for key, sku in sku_map.items():
        parts = [p for p in str(key).split(";") if p]
        if parts and all(p in valid for p in parts):
            if isinstance(sku, dict):
                sku = dict(sku)
                sku["isDelete"] = "0" if str(sku.get("isDelete") or "0") not in ("1", "true", "True") else "1"
            kept[key] = sku
        else:
            dropped += 1
    product["skuMap"] = kept
    return {"kept": len(kept), "dropped": dropped, "valid_ids": len(valid)}


def fill_main_images(
    product: dict[str, Any],
    blank_tee_dir: Path,
    target: int = 9,
    fill_image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """补主图到 target 张。

    开放平台不支持本地上传：只能写入公网 URL（图床）。
    优先用 fill_image_urls（按顺序）；没有 URL 时仅记录本地待传路径，不写入 imgUrls。
    """
    imgs = product.get("imgUrls") or product.get("main_images") or []
    if isinstance(imgs, str):
        imgs = [imgs]
    imgs = [str(u) for u in imgs if u]
    need = max(0, target - len(imgs))
    note: dict[str, Any] = {"current": len(imgs), "need": need, "added_urls": [], "added_local": []}
    if need <= 0:
        product["imgUrls"] = imgs
        return note

    url_pool = [str(u).strip() for u in (fill_image_urls or []) if str(u).strip().startswith("http")]
    if url_pool:
        added: list[str] = []
        i = 0
        while len(added) < need:
            added.append(url_pool[i % len(url_pool)])
            i += 1
        imgs.extend(added)
        product["imgUrls"] = imgs
        note["added_urls"] = added
        note["mode"] = "url_pool"
        return note

    pool = []
    if blank_tee_dir.exists():
        pool = sorted(
            [p for p in blank_tee_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 1024],
            key=lambda p: p.name.lower(),
        )
    if not pool:
        note["warning"] = "无图床 URL，且本地通用主图目录为空/仅占位图；跳过补图"
        product["imgUrls"] = imgs
        note["mode"] = "skipped"
        return note

    added_local = []
    i = 0
    while len(added_local) < need:
        added_local.append(str(pool[i % len(pool)]))
        i += 1
    note["added_local"] = added_local
    note["mode"] = "local_pending_need_cdn"
    note["warning"] = "开放平台需图床 URL；本地图已选好但未写入 imgUrls"
    product["imgUrls"] = imgs
    product["_pending_local_images"] = added_local
    return note
