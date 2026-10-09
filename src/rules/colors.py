from __future__ import annotations

import re
from typing import Any


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


COLOR_ALIASES: dict[str, list[str]] = {
    "black": ["negro", "negra", "black", "黑色", "黑", "preto"],
    "white": ["blanco", "blanca", "white", "白色", "白", "branco"],
    "red": ["rojo", "roja", "red", "红色", "红", "vermelho"],
    "pink": ["rosa", "rosado", "rosada", "pink", "粉色", "粉"],
    "blue": ["azul", "blue", "蓝色", "蓝"],
    "navy": ["azul marino", "navy", "navy blue", "azul oscuro", "深蓝", "藏青", "dark blue"],
    "light_blue": ["azul claro", "celeste", "light blue", "浅蓝", "宝蓝", "royal blue"],
}

ES_NAME = {
    "black": "Negro",
    "white": "Blanco",
    "red": "Rojo",
    "pink": "Rosa",
    "blue": "Azul",
    "navy": "Azul Marino",
    "light_blue": "Azul claro",
}

_SKU_CODE = re.compile(r"^[A-Za-z]{1,6}\d{2,}(?:[-_]\d+)?$", re.I)
_DIGIT_ONLY = re.compile(r"^\d{1,3}$")
_STYLE_DASH = re.compile(r"^[A-Za-z]{1,8}\d*[-_]\d+", re.I)


# 库外色/非纯黑（水洗黑、灰黑、炭黑等）→ 不当成可上架黑
_DENY_COLOR_MARKERS = re.compile(
    r"lavad[oa]|washed|deslavad[oa]|decolorad[oa]|acid\s*wash|stone\s*wash|"
    r"charcoal|heather|carb[oó]n|"
    r"gris\s*negr|negr[oa]\s*gris|gray\s*black|black\s*gray|grey\s*black|"
    r"negr[oa]\s*lavad|black\s*wash|washed\s*black|faded\s*black|"
    r"\bverde\b|\bgreen\b|\bgris\b|\bgray\b|\bgrey\b|\bamarill[oa]\b|\byellow\b|"
    r"\bnaranj[oa]\b|\borange\b|\bmorad[oa]\b|\bpurple\b|\bbeige\b|\bkhaki\b|"
    r"\b绿色\b|\b灰色\b|\b灰黑\b|\b炭黑\b|\b水洗黑\b|\b黄色\b|\b橙色\b|\b紫色\b|\b水洗",
    re.I,
)


def match_color(value: str) -> str | None:
    n = _n(value)
    # 水洗黑 / 绿色等明确不要 → 当作未匹配，交给 filter 删除
    if _DENY_COLOR_MARKERS.search(n):
        return None
    # longer aliases first
    pairs = []
    for key, aliases in COLOR_ALIASES.items():
        for a in aliases + [ES_NAME.get(key, key)]:
            pairs.append((a, key))
    pairs.sort(key=lambda x: -len(x[0]))
    for a, key in pairs:
        an = _n(a)
        if n == an:
            return key
        # "Negro (impreso)" / "Negro (color sólido)" → black
        if len(an) >= 4 and (n.startswith(an + " ") or n.startswith(an + "(") or n.startswith(an + "-")):
            return key
        if len(an) >= 4 and an in n and len(n) <= len(an) + 18:
            return key
        # "Negro18" / "Blanco-S" / "NEGRA_01" → 色名嵌在款号前缀
        if len(an) >= 4 and re.match(rf"^{re.escape(an)}[\s\-_]?\d", n):
            return key
        if len(an) >= 4 and re.match(rf"^{re.escape(an)}[\s\-_]?[smlx0-9]", n):
            return key
    return None


def looks_like_style_code(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    if _DIGIT_ONLY.match(v):
        return True
    if _STYLE_DASH.match(v):
        return True
    return bool(_SKU_CODE.match(v) or re.match(r"^YTS?\d+", v, re.I))


def filter_colors(product: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    allowed = [
        str(x).lower()
        for x in (scheme.get("allowed_colors") or ["black", "white", "red", "blue", "pink"])
    ]
    report: dict[str, Any] = {"mode": "", "deleted": [], "kept": [], "allowed": allowed}

    props = product.get("skuPropertyList") or []
    color_prop = None
    color_idx = None
    for i, prop in enumerate(props):
        name = str(prop.get("name") or prop.get("attrName") or "")
        low = name.lower()
        if "color" in low or "colour" in low or "color" == low or "颜色" in name or "colou" in low:
            color_prop = prop
            color_idx = i
            break
    # fallback: first non-size axis
    if color_prop is None:
        for i, prop in enumerate(props):
            name = str(prop.get("name") or prop.get("attrName") or "").lower()
            if "talla" in name or "size" in name or "尺码" in name:
                continue
            color_prop, color_idx = prop, i
            break

    if color_prop is None:
        report["mode"] = "no_color_axis"
        return report

    values = color_prop.get("attrValueList") or []
    labels = []
    for v in values:
        if isinstance(v, dict):
            labels.append(str(v.get("attrValue") or v.get("value") or ""))
        else:
            labels.append(str(v))

    style_hits = sum(1 for x in labels if looks_like_style_code(x))
    color_hits = sum(1 for x in labels if match_color(x))
    if style_hits and (style_hits >= color_hits or not color_hits):
        report["mode"] = "skip_style_codes"
        report["kept"] = labels
        return report

    new_vals = []
    deleted = []
    deleted_ids: list[str] = []
    kept = []
    for v in values:
        label = str(v.get("attrValue") or v.get("value") or "") if isinstance(v, dict) else str(v)
        key = match_color(label)
        if key and key in allowed:
            item = dict(v) if isinstance(v, dict) else {"attrValue": label}
            item["isDelete"] = False
            item["attrValue"] = item.get("attrValue") or label
            new_vals.append(item)
            kept.append(label)
        else:
            deleted.append(label)
            if isinstance(v, dict):
                vid = str(v.get("attrValueId") or "")
                if vid:
                    deleted_ids.append(vid)
                dead = dict(v)
                dead["isDelete"] = True
                new_vals.append(dead)

    if not deleted:
        report["mode"] = "keep_all_in_library"
        report["kept"] = kept
        return report

    # 若库内颜色一个都不剩：款号轴回退；水洗黑/绿等明确库外色则真删，交给 SKU 门禁整链留库
    if not kept:
        explicit_out = any(_DENY_COLOR_MARKERS.search(_n(x)) for x in labels)
        if not explicit_out:
            report["mode"] = "keep_all_no_library_hit"
            report["kept"] = labels
            report["deleted"] = []
            report["note"] = "过滤后无保留色，回退为保留全部色/款号轴"
            return report

    color_prop["attrValueList"] = [x for x in new_vals if not x.get("isDelete")]
    props[color_idx] = color_prop
    product["skuPropertyList"] = props

    # mark SKUs whose key contains deleted color attrValueId
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

    report["mode"] = "delete_out_of_library"
    report["deleted"] = deleted
    report["deleted_ids"] = deleted_ids
    report["kept"] = kept
    return report
