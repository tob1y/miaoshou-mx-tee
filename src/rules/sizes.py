from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


READ_ALIASES = {
    "s": "S",
    "small": "S",
    "chica": "S",
    "ch": "S",  # MX
    "eg": "S",  # extra chica (偶见)
    "m": "M",
    "medium": "M",
    "mediana": "M",
    "l": "L",
    "large": "L",
    "grande": "L",
    "g": "L",  # MX
    "xl": "XL",
    "extragrande": "XL",
    "x-large": "XL",
    "xlarge": "XL",
    "xg": "XL",  # MX
    "xxl": "XXL",
    "2xl": "XXL",
    "2xl.": "XXL",
    "xxlarge": "XXL",
    "xx-large": "XXL",
    "2x-large": "XXL",
    "extraextragrande": "XXL",
    "xxg": "XXL",  # MX
    "xxg.": "XXL",
    "xxxl": "XXXL",
    "3xl": "XXXL",
    "xxxlarge": "XXXL",
    "xxx-large": "XXXL",
    "3x-large": "XXXL",
    "xxxg": "XXXL",  # MX
    "xxxg.": "XXXL",
    "4xl": "4XL",
    "xxxxl": "4XL",
    "xxxxg": "4XL",  # MX
    "5xl": "5XL",
    "xxxxxl": "5XL",
    "6xl": "6XL",
    "xxxxxxl": "6XL",
}

ONE_SIZE = {
    "unitalla",
    "unitalla/",
    "tallaunica",
    "tallaunica/",
    "unica",
    "unica/",
    "onesize",
    "one-size",
    "onesizeonly",
    "freesize",
    "free-size",
    "均码",
    "均碼",
}


def normalize_size_token(raw: str) -> str | None:
    key = _n(raw).replace(" ", "").replace("talla", "")
    if key in ONE_SIZE:
        return "S"
    return READ_ALIASES.get(key)


def write_label(canon: str, style: str = "2xl") -> str:
    if style == "xxl":
        return {"XXL": "XXL", "XXXL": "XXXL"}.get(canon, canon)
    return {"XXL": "2XL", "XXXL": "3XL"}.get(canon, canon)


def generated_attr_value_id(label: str) -> str:
    """CONFIRMED: md5(utf-8 label).hexdigest()[:10]"""
    return hashlib.md5(str(label).encode("utf-8")).hexdigest()[:10]


def _value_label(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("attrValue") or v.get("value") or v.get("name") or "")
    return str(v or "")


def _is_size_prop(prop: dict[str, Any]) -> bool:
    name = str(prop.get("name") or prop.get("attrName") or prop.get("attributeNameAlias") or "")
    low = name.lower()
    vals = prop.get("attrValueList") or prop.get("values") or []
    if not vals:
        return "talla" in low or "size" in low or "尺码" in name
    hits = 0
    for v in vals:
        lab = _value_label(v)
        if normalize_size_token(lab):
            hits += 1
    # 以属性值是否像尺码为准；避免「Talla」轴实际是款号、「Color」轴才是 S/M/L
    ratio = hits / max(1, len(vals))
    if ratio >= 0.5:
        return True
    if hits == 0 and ("talla" in low or "size" in low or "尺码" in name):
        return False
    return False


def _find_size_prop(props: list[Any]) -> tuple[int | None, dict[str, Any] | None]:
    best_i = None
    best_score = -1.0
    best_prop = None
    name_fallback: tuple[int, dict[str, Any]] | None = None
    for i, prop in enumerate(props):
        if not isinstance(prop, dict):
            continue
        vals = prop.get("attrValueList") or prop.get("values") or []
        name = str(prop.get("name") or prop.get("attrName") or "").lower()
        is_named = "talla" in name or "size" in name or "尺码" in name
        if is_named and name_fallback is None:
            name_fallback = (i, prop)
        if not vals:
            continue
        hits = sum(1 for v in vals if normalize_size_token(_value_label(v)))
        ratio = hits / len(vals)
        score = ratio * 10 + (1 if is_named else 0)
        if ratio >= 0.5 and score > best_score:
            best_score = score
            best_i = i
            best_prop = prop
    if best_prop is not None:
        return best_i, best_prop
    # 名称是 Talla/Size 但取值是童装码/字母款号时，仍复用该轴，避免再新建导致「Talla重复」
    if name_fallback is not None:
        return name_fallback[0], name_fallback[1]
    return None, None


def _dedupe_named_size_axes(props: list[Any], keep_idx: int) -> list[Any]:
    """Keep one size axis; drop extra props also named Talla/Size."""
    out: list[Any] = []
    for i, prop in enumerate(props):
        if not isinstance(prop, dict):
            out.append(prop)
            continue
        if i == keep_idx:
            out.append(prop)
            continue
        name = str(prop.get("name") or prop.get("attrName") or "").lower()
        if "talla" in name or "size" in name or "尺码" in name:
            continue
        out.append(prop)
    return out


def plan_and_apply_sizes(product: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    """Mutate skuPropertyList + skuMap sizes toward scheme target. Returns report."""
    target_write = scheme.get("sizes") or ["S", "M", "L", "XL", "2XL", "3XL"]
    write_style = scheme.get("size_write_style") or "2xl"
    target_canon: list[str] = []
    for t in target_write:
        c = normalize_size_token(t) or t.upper()
        if c == "2XL":
            c = "XXL"
        if c == "3XL":
            c = "XXXL"
        target_canon.append(c)

    report: dict[str, Any] = {
        "added": [],
        "deleted": [],
        "target": [write_label(c, write_style) for c in target_canon],
    }

    props = list(product.get("skuPropertyList") or [])
    size_idx, size_prop = _find_size_prop(props)

    if size_prop is None:
        size_prop = {"name": "Talla", "attrName": "Talla", "attrValueList": []}
        props.append(size_prop)
        size_idx = len(props) - 1
        report["created_axis"] = True
    else:
        # 统一轴名，避免 Size + Talla 并存
        size_prop = dict(size_prop)
        size_prop["name"] = "Talla"
        size_prop["attrName"] = "Talla"
        props[size_idx] = size_prop
        props = _dedupe_named_size_axes(props, size_idx)
        # re-find index after dedupe
        size_idx, size_prop = _find_size_prop(props)
        report["reused_named_axis"] = True

    old_values = list(size_prop.get("attrValueList") or size_prop.get("values") or [])
    old_by_canon: dict[str, dict[str, Any]] = {}
    for v in old_values:
        if not isinstance(v, dict):
            continue
        lab = _value_label(v)
        canon = normalize_size_token(lab)
        if not canon:
            continue
        if canon == "2XL":
            canon = "XXL"
        if canon == "3XL":
            canon = "XXXL"
        old_by_canon[canon] = v
        if canon not in target_canon:
            report["deleted"].append(lab)

    new_values: list[dict[str, Any]] = []
    id_remap: dict[str, str] = {}
    for canon in target_canon:
        label = write_label(canon, write_style)
        prev = old_by_canon.get(canon)
        if prev:
            entry = dict(prev)
            old_lab = str(prev.get("attrValue") or "")
            old_id = str(prev.get("attrValueId") or generated_attr_value_id(old_lab))
            # keep stable id when write label already matches
            if old_lab == label and old_id:
                new_id = old_id
            else:
                new_id = generated_attr_value_id(label)
                if old_id and old_id != new_id:
                    id_remap[old_id] = new_id
            entry["attrValue"] = label
            entry["attrValueId"] = new_id
            entry["isDelete"] = False
        else:
            report["added"].append(label)
            new_id = generated_attr_value_id(label)
            entry = {
                "attrValue": label,
                "attrValueId": new_id,
                "isDelete": False,
            }
        new_values.append(entry)

    size_prop["attrValueList"] = new_values
    if not size_prop.get("name") and not size_prop.get("attrName"):
        size_prop["name"] = "Talla"
    # 尺码轴若只剩部分带图，平台会报「请上传全部选项图片」；统一清空尺码选项图
    for entry in new_values:
        for k in ("imgUrl", "imageUrl", "picUrl", "supplementarySkuImageUrls"):
            if k in entry:
                entry[k] = [] if k == "supplementarySkuImageUrls" else ""
    props[size_idx] = size_prop
    product["skuPropertyList"] = props

    sku_map = product.get("skuMap") or {}
    if isinstance(sku_map, dict) and sku_map:
        product["skuMap"] = _rebuild_sku_map_by_ids(sku_map, old_values, new_values, id_remap)

    return report


def _ids_in_key(key: str) -> list[str]:
    return [p for p in str(key).split(";") if p]


def _rebuild_sku_map_by_ids(
    sku_map: dict[str, Any],
    old_size_values: list[Any],
    new_size_values: list[dict[str, Any]],
    id_remap: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Rebuild skuMap keys as ;nonSizeIds...;sizeId; with isDelete '0'/'1'."""
    id_remap = id_remap or {}
    old_size_ids = set(id_remap.keys())
    for v in old_size_values:
        if isinstance(v, dict) and v.get("attrValueId"):
            old_size_ids.add(str(v["attrValueId"]))
        lab = _value_label(v)
        if lab:
            old_size_ids.add(generated_attr_value_id(lab))
            c = normalize_size_token(lab)
            if c:
                old_size_ids.add(generated_attr_value_id(write_label(c, "2xl")))
                old_size_ids.add(generated_attr_value_id(write_label(c, "xxl")))

    new_size_ids = [str(v["attrValueId"]) for v in new_size_values if v.get("attrValueId")]
    all_size_ids = set(old_size_ids) | set(new_size_ids) | set(id_remap.values())
    if not new_size_ids:
        return sku_map

    active = [(k, v) for k, v in sku_map.items() if isinstance(v, dict) and not _truthy_delete(v)]
    if not active:
        return sku_map

    def norm_delete(sku: dict[str, Any], deleted: bool) -> dict[str, Any]:
        out = dict(sku)
        # Miaoshou samples use string flags
        out["isDelete"] = "1" if deleted else "0"
        return out

    def make_key(other: tuple[str, ...], size_id: str) -> str:
        # Official: ;id1;id2; with attr value IDs sorted ascending
        parts = sorted([*other, size_id], key=lambda x: x)
        return ";" + ";".join(parts) + ";"

    groups: dict[tuple[str, ...], list[tuple[str, dict, str | None]]] = {}
    for key, sku in active:
        ids = _ids_in_key(key)
        size_id = next((i for i in ids if i in all_size_ids), None)
        if size_id and size_id in id_remap:
            size_id = id_remap[size_id]
        other = tuple(i for i in ids if i not in all_size_ids)
        groups.setdefault(other, []).append((key, sku, size_id))

    out: dict[str, Any] = {}
    used_old_keys: set[str] = set()
    for other, rows in groups.items():
        template = rows[0][1]
        existing_size: dict[str, dict] = {}
        for k, s, sid in rows:
            if sid:
                existing_size[id_remap.get(sid, sid)] = s
                used_old_keys.add(k)
        for sid in new_size_ids:
            src = existing_size.get(sid, template)
            out[make_key(other, sid)] = norm_delete(json.loads(json.dumps(src)), False)

    # keep untouched non-size-related entries; drop replaced size keys (do not keep orphans)
    for key, sku in sku_map.items():
        if key in out:
            continue
        ids = _ids_in_key(key)
        if any(i in all_size_ids for i in ids):
            # old size combination superseded — omit (avoid 无对应规格SKU)
            continue
        elif isinstance(sku, dict):
            out[key] = sku
    return out


def _truthy_delete(sku: dict[str, Any]) -> bool:
    v = sku.get("isDelete")
    return v in (True, 1, "1", "true", "True")
