"""Filter long-sleeve (and similar) SKU axis values — keep short-sleeve only."""
from __future__ import annotations

import re
from typing import Any


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# 默认：命中则删除（长袖等）
DEFAULT_DELETE = [
    "manga larga",
    "manga larga.",
    "long sleeve",
    "long-sleeve",
    "longsleeve",
    "长袖",
    "長袖",
    "full sleeve",
    "fullsleeve",
]

# 默认：命中则保留（短袖等）；若轴上既有删又有留，优先按删除名单删
DEFAULT_KEEP = [
    "manga corta",
    "short sleeve",
    "short-sleeve",
    "shortsleeve",
    "短袖",
    " sleeveless",  # unlikely on tee axis; kept configurable
]


def _is_sleeve_axis(name: str) -> bool:
    n = _n(name)
    keys = (
        "manga",
        "sleeve",
        "袖长",
        "袖長",
        "袖型",
        "tipo de manga",
        "largo de manga",
        "sleeve length",
        "sleeve type",
    )
    return any(k in n for k in keys)


def _label_of(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("attrValue") or v.get("value") or "")
    return str(v or "")


def _hit(label: str, keywords: list[str]) -> bool:
    n = _n(label)
    if not n:
        return False
    for kw in keywords:
        k = _n(kw)
        if not k:
            continue
        if k in n or n == k:
            return True
    return False


def filter_sleeves(product: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    """删除长袖等 SKU 规格值；无袖长轴则 no-op。

    scheme:
      delete_sleeves: true|false (default true)
      delete_sleeve_keywords: [...]
      keep_sleeve_keywords: [...]  # 仅用于报告；删除以 delete 名单为准
    """
    report: dict[str, Any] = {"mode": "", "deleted": [], "kept": [], "axis": None}
    if scheme.get("delete_sleeves") is False:
        report["mode"] = "disabled"
        return report

    delete_kw = [str(x) for x in (scheme.get("delete_sleeve_keywords") or DEFAULT_DELETE)]
    keep_kw = [str(x) for x in (scheme.get("keep_sleeve_keywords") or DEFAULT_KEEP)]

    props = product.get("skuPropertyList") or []
    sleeve_prop = None
    sleeve_idx = None
    for i, prop in enumerate(props):
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("name") or prop.get("attrName") or "")
        if _is_sleeve_axis(name):
            sleeve_prop, sleeve_idx = prop, i
            break

    # 有的源站把长短袖塞进 Style/Estilo/Tipo，不叫 manga
    if sleeve_prop is None:
        for i, prop in enumerate(props):
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name") or prop.get("attrName") or "").lower()
            if "talla" in name or "size" in name or "尺码" in name:
                continue
            if "color" in name or "colour" in name or "颜色" in name:
                continue
            labels = [_label_of(v) for v in (prop.get("attrValueList") or [])]
            if any(_hit(lb, delete_kw) or _hit(lb, keep_kw) for lb in labels):
                sleeve_prop, sleeve_idx = prop, i
                report["axis_guess"] = str(prop.get("name") or prop.get("attrName") or "")
                break

    if sleeve_prop is None:
        report["mode"] = "no_sleeve_axis"
        return report

    report["axis"] = str(sleeve_prop.get("name") or sleeve_prop.get("attrName") or "")
    values = list(sleeve_prop.get("attrValueList") or [])
    new_vals: list[dict] = []
    deleted: list[str] = []
    deleted_ids: list[str] = []
    kept: list[str] = []

    for v in values:
        label = _label_of(v)
        item = dict(v) if isinstance(v, dict) else {"attrValue": label}
        if _hit(label, delete_kw):
            item["isDelete"] = True
            deleted.append(label)
            vid = str(item.get("attrValueId") or "")
            if vid:
                deleted_ids.append(vid)
            new_vals.append(item)
        else:
            item["isDelete"] = False
            kept.append(label)
            new_vals.append(item)

    if not deleted:
        report["mode"] = "nothing_to_delete"
        report["kept"] = kept
        return report

    # 若删完后一个不留，回退（避免整商品 SKU 被删光）
    if not kept:
        report["mode"] = "abort_would_empty"
        report["deleted"] = deleted
        report["kept"] = [_label_of(v) for v in values]
        report["note"] = "删除长袖后无剩余规格，已跳过"
        return report

    sleeve_prop = dict(sleeve_prop)
    sleeve_prop["attrValueList"] = [x for x in new_vals if not x.get("isDelete")]
    props[sleeve_idx] = sleeve_prop
    product["skuPropertyList"] = props

    sku_map = product.get("skuMap") or {}
    if isinstance(sku_map, dict) and deleted_ids:
        dead = set(deleted_ids)
        for key, sku in list(sku_map.items()):
            if not isinstance(sku, dict):
                continue
            parts = [p for p in str(key).split(";") if p]
            if any(p in dead for p in parts):
                sku = dict(sku)
                sku["isDelete"] = "1"
                sku_map[key] = sku
        product["skuMap"] = sku_map

    report["mode"] = "delete_long_sleeve"
    report["deleted"] = deleted
    report["deleted_ids"] = deleted_ids
    report["kept"] = kept
    return report
