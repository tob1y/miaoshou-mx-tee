"""Pre-publish local sanitization matching 妙手「发布产品」弹窗选项."""
from __future__ import annotations

from typing import Any


def _cut(s: str, max_chars: int, from_end: bool = True) -> str:
    t = str(s or "")
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    return t[:max_chars] if from_end else t[-max_chars:]


def apply_publish_prep(product: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Mutate product copy fields for package / title / SKU truncations. Returns report."""
    report: dict[str, Any] = {"actions": []}

    pkg = cfg.get("package") or {}
    if pkg.get("weight_kg") is not None:
        product["weight"] = float(pkg["weight_kg"])
        report["actions"].append(f"weight={product['weight']}")
    if pkg.get("length_cm") is not None:
        product["packageLength"] = float(pkg["length_cm"])
        report["actions"].append(f"length={product['packageLength']}")
    if pkg.get("width_cm") is not None:
        product["packageWidth"] = float(pkg["width_cm"])
        report["actions"].append(f"width={product['packageWidth']}")
    if pkg.get("height_cm") is not None:
        product["packageHeight"] = float(pkg["height_cm"])
        report["actions"].append(f"height={product['packageHeight']}")

    title_cfg = cfg.get("product_title") or {}
    if title_cfg.get("truncate_enabled"):
        max_c = int(title_cfg.get("max_chars") or 255)
        from_end = str(title_cfg.get("truncate_from") or "end") == "end"
        old = str(product.get("title") or "")
        new = _cut(old, max_c, from_end=from_end)
        if new != old:
            product["title"] = new
            report["actions"].append(f"title_truncated {len(old)}->{len(new)}")

    sku_cfg = cfg.get("sku_spec") or {}
    from_end = str(sku_cfg.get("truncate_from") or "end") == "end"
    props = product.get("skuPropertyList") or []
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            if sku_cfg.get("attr_name_truncate"):
                max_n = int(sku_cfg.get("attr_name_max_chars") or 20)
                for key in ("attrName", "name"):
                    if prop.get(key):
                        prop[key] = _cut(str(prop[key]), max_n, from_end=from_end)
            if sku_cfg.get("attr_value_truncate"):
                max_v = int(sku_cfg.get("attr_value_max_chars") or 50)
                for v in prop.get("attrValueList") or []:
                    if isinstance(v, dict) and v.get("attrValue"):
                        v["attrValue"] = _cut(str(v["attrValue"]), max_v, from_end=from_end)

    # notes / detail truncate soft cap (UI: 超过最大字符限制时自动截断)
    detail_cfg = cfg.get("product_detail") or {}
    if detail_cfg.get("truncate_over_limit"):
        notes = str(product.get("notes") or "")
        # TikTok/妙手详情常见上限约 10000；保守截断防保存失败
        max_notes = int(detail_cfg.get("max_chars") or 10000)
        if len(notes) > max_notes:
            product["notes"] = notes[:max_notes]
            report["actions"].append(f"notes_truncated {len(notes)}->{max_notes}")

    # platform SKU truncate
    plat = cfg.get("platform_sku") or {}
    if plat.get("truncate_enabled"):
        from_end = str(plat.get("truncate_from") or "end") == "end"
        max_sku = int(plat.get("max_chars") or 50)
        sku_map = product.get("skuMap") or {}
        if isinstance(sku_map, dict):
            for sku in sku_map.values():
                if not isinstance(sku, dict):
                    continue
                for key in ("platformSku", "sellerSku", "outerSku", "skuCode"):
                    if sku.get(key):
                        sku[key] = _cut(str(sku[key]), max_sku, from_end=from_end)

    report["ok"] = True
    return report


def build_publish_api_body(
    detail_ids: list[int],
    shop_ids: list[int],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Official OpenAPI confirmed: shopIds + detailIds.

    Extra UI options are attached under publishOptions for forward-compat /
    documentation; server may ignore unknown fields.
    """
    mode = str(cfg.get("publish_mode") or "immediate")
    body: dict[str, Any] = {
        "shopIds": [int(x) for x in shop_ids],
        "detailIds": [int(x) for x in detail_ids],
    }
    # Best-effort extras mirrored from UI (may be ignored by OpenAPI)
    body["publishOptions"] = {
        "site": cfg.get("site") or "MX",
        "publishMode": mode,  # immediate | schedule
        "scheduleAt": cfg.get("schedule_at"),
        "package": cfg.get("package") or {},
        "autoTranslate": cfg.get("auto_translate") or {},
        "autoAdsAfterPublish": bool(cfg.get("auto_ads_after_publish")),
        "antiDuplicate": cfg.get("anti_duplicate") or {},
        "productDetail": cfg.get("product_detail") or {},
        "productTitle": cfg.get("product_title") or {},
        "skuSpec": cfg.get("sku_spec") or {},
        "platformSku": cfg.get("platform_sku") or {},
        "prohibitedWords": cfg.get("prohibited_words") or {},
        "price": cfg.get("price") or {},
    }
    return body
