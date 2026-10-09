"""上架门禁：错品类 / 随机装 / SKU 健康度。"""
from __future__ import annotations

import re
from typing import Any

from rules.titles import is_multipack_text


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# 短袖T恤店（男女均可）：命中则留库不上架
# 女装 T 恤允许；童装 / 卫衣 / 长袖 / 非T恤品类仍拦
DEFAULT_BLOCK_TITLE = [
    r"\bpara\s+ni[nñ][oa]s?\b",
    r"\bni[nñ][oa]s?\b",
    r"\bkids?\b",
    r"\bchild(?:ren)?\b",
    r"\bbaby\b",
    r"\bbeb[eé]s?\b",
    r"\bhoodie\b",
    r"\bsudadera\b",
    r"\bsweatshirt\b",
    r"\bpolar\b",
    r"\bhoodie[ds]?\b",
    r"\bpolo\b",
    r"\bcamis[ao]\b",  # 衬衫/衬衣，非 T 恤
    r"\bblusa\b",
    r"\btank\s*top\b",
    r"\bsin\s+mangas\b",
    r"\bsleeveless\b",
    r"\bvestido\b",
    r"\bpantal[oó]n(?:es)?\b",
    r"\bshorts\b",  # 勿用 short，会误伤 short sleeve
    r"\bfalda\b",
    r"\bchaqueta\b",
    r"\bjacket\b",
    r"\babrigo\b",
    r"\b童装\b",
    r"\b卫衣\b",
    r"\b连帽\b",
    r"\b背心\b",
    r"\b连衣裙\b",
    # 成套/套装（上下装成套；多件装另有 multipack 门禁）
    # 中文不用 \\b：邻接汉字时 \\b 会失效
    r"套装",
    r"成套",
    r"两件套",
    r"三件套",
    r"\bconjuntos?\b",
    r"\boutfits?\b",
    r"\bco[- ]?ords?\b",
    r"\bmatching\s+set\b",
    r"\bset\s+de\s+(?:pantal|short|falda|blusa|camis|top|sudadera)",
    r"\btraje\s+deportivo\b",
    r"\btracks?uit\b",
    # 长袖：整链不上架（不只删 SKU）
    r"\bmanga\s+larga\b",
    r"\blong[\s\-]?sleeve\b",
    r"\blongsleeve\b",
    r"\bfull[\s\-]?sleeve\b",
    r"\b长袖\b",
    r"\b長袖\b",
    # 紧身/压缩短袖：运动压缩衣不上（例：Camiseta de compresión …）
    r"\bcompresi[oó]n\b",
    r"\bcompression\b",
    r"\bcomprimida\b",
    r"\btight[\s\-]?fit(?:ting)?\b",
    r"\bbody[\s\-]?fit\b",
    r"\bmuscle[\s\-]?fit\b",
    r"\bskin[\s\-]?tight\b",
    r"\bcamiseta\s+de\s+compresi[oó]n\b",
    r"\b紧身\b",
    r"\b压缩衣\b",
    r"\b压缩衫\b",
]

# 随机发货 / 随机色款：不要当正常单件上
DEFAULT_RANDOM_TITLE = [
    r"\baleatori[oa]s?\b",
    r"\bal\s+azar\b",
    r"\brandom\b",
    r"\bsorpresa\b",
    r"\bpack\s+sorpresa\b",
    r"\bcolores?\s+aleatori",
    r"\bestilos?\s+aleatori",
    r"\bdise[nñ]os?\s+aleatori",
]


def should_hold_wrong_category(
    product: dict[str, Any],
    scheme: dict[str, Any] | None = None,
    *,
    original_title: str | None = None,
) -> dict[str, Any]:
    scheme = scheme or {}
    enabled = scheme.get("hold_wrong_category", True)
    title = original_title if original_title is not None else str(
        product.get("oriTitle") or product.get("title") or ""
    )
    report: dict[str, Any] = {
        "enabled": bool(enabled),
        "hold": False,
        "title": title[:140],
        "matched": [],
        "reason": "",
    }
    if not enabled:
        report["reason"] = "disabled"
        return report

    patterns = list(scheme.get("block_title_patterns") or DEFAULT_BLOCK_TITLE)
    t = _n(title)
    hits: list[str] = []
    for pat in patterns:
        try:
            if re.search(pat, t, flags=re.I):
                hits.append(pat)
        except re.error:
            continue
    if hits:
        report["hold"] = True
        report["matched"] = hits[:8]
        # 紧身/压缩单独标原因，方便飞书复核
        if any("compresi" in p or "compression" in p or "紧身" in p or "压缩" in p or "tight" in p for p in hits):
            report["reason"] = "compression_or_tight_fit"
        else:
            report["reason"] = "wrong_category_or_audience"
    else:
        report["reason"] = "ok"
    return report


# 规格名里出现这些，整条链接留库，不删 SKU
HOODIE_MARKERS = (
    "sudadera",
    "hoodie",
    "sweatshirt",
    "capucha",
    "卫衣",
    "连帽",
)


def _option_labels(product: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for prop in product.get("skuPropertyList") or []:
        if not isinstance(prop, dict):
            continue
        for v in prop.get("attrValueList") or []:
            if isinstance(v, dict):
                labels.append(str(v.get("attrValue") or v.get("name") or v.get("value") or ""))
            else:
                labels.append(str(v))
    for key, val in (product.get("colorMap") or {}).items():
        if isinstance(val, dict):
            labels.append(str(val.get("name") or key))
        else:
            labels.append(str(key))
    return labels


def _hoodie_marker_hits(product: dict[str, Any], title: str) -> list[str]:
    hits: list[str] = []
    for text in [title, *_option_labels(product)]:
        n = _n(text)
        if not n:
            continue
        if any(mark in n for mark in HOODIE_MARKERS):
            hits.append(text[:80])
    return hits


def should_hold_hoodie(
    product: dict[str, Any],
    scheme: dict[str, Any] | None = None,
    *,
    original_title: str | None = None,
) -> dict[str, Any]:
    """标题或任一规格出现卫衣 → 整链留库，不改规格。（短袖 T 专线默认开）"""
    scheme = scheme or {}
    title = original_title if original_title is not None else str(
        product.get("oriTitle") or product.get("title") or ""
    )
    report: dict[str, Any] = {
        "gate": "hoodie",
        "enabled": scheme.get("hold_hoodie", True) is not False,
        "hold": False,
        "matched": [],
        "reason": "ok",
    }
    if not report["enabled"]:
        report["reason"] = "disabled"
        return report
    hits = _hoodie_marker_hits(product, title)
    if hits:
        report["hold"] = True
        report["matched"] = hits[:8]
        report["reason"] = "hoodie"
    return report


def should_require_hoodie(
    product: dict[str, Any],
    scheme: dict[str, Any] | None = None,
    *,
    original_title: str | None = None,
) -> dict[str, Any]:
    """卫衣专线：标题/规格必须带卫衣标记，否则整链留库。"""
    scheme = scheme or {}
    title = original_title if original_title is not None else str(
        product.get("oriTitle") or product.get("title") or ""
    )
    report: dict[str, Any] = {
        "gate": "require_hoodie",
        "enabled": scheme.get("require_hoodie") is True,
        "hold": False,
        "matched": [],
        "reason": "ok",
    }
    if not report["enabled"]:
        report["reason"] = "disabled"
        return report
    hits = _hoodie_marker_hits(product, title)
    if hits:
        report["matched"] = hits[:8]
        report["reason"] = "ok"
        return report
    report["hold"] = True
    report["reason"] = "not_hoodie"
    return report


def _pack_qty_from_product(product: dict[str, Any]) -> str:
    for a in product.get("productAttributes") or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("attributeId") or "") != "100347":
            continue
        vals = a.get("attributeValues") or []
        names = [
            str(v.get("valueName") or v.get("name") or "").strip()
            for v in vals
            if isinstance(v, dict)
        ]
        names = [n for n in names if n]
        return ",".join(names) if names else ""
    return ""


def should_hold_multipack(
    product: dict[str, Any],
    scheme: dict[str, Any] | None = None,
    *,
    original_title: str | None = None,
) -> dict[str, Any]:
    """多件装链接：直接不上架（不要改成1件再发）。"""
    scheme = scheme or {}
    enabled = scheme.get("hold_multipack", True)
    title = original_title if original_title is not None else str(
        product.get("oriTitle") or product.get("title") or ""
    )
    report: dict[str, Any] = {
        "enabled": bool(enabled),
        "hold": False,
        "title": title[:140],
        "pack_qty": "",
        "reason": "",
    }
    if not enabled:
        report["reason"] = "disabled"
        return report

    qty = _pack_qty_from_product(product)
    report["pack_qty"] = qty
    title_mp = is_multipack_text(title)
    qty_bad = bool(qty) and qty.strip() not in ("1",)
    if title_mp or qty_bad:
        report["hold"] = True
        report["reason"] = "multipack"
        report["title_multipack"] = title_mp
        report["qty_bad"] = qty_bad
    else:
        report["reason"] = "ok"
    return report


def should_hold_random_assortment(
    product: dict[str, Any],
    scheme: dict[str, Any] | None = None,
    *,
    original_title: str | None = None,
) -> dict[str, Any]:
    """随机装 / 随机色款：留库不上架（标题可先去掉 aleatorio 词，但仍不发）。"""
    scheme = scheme or {}
    enabled = scheme.get("hold_random_assortment", True)
    title = original_title if original_title is not None else str(
        product.get("oriTitle") or product.get("title") or ""
    )
    report: dict[str, Any] = {
        "enabled": bool(enabled),
        "hold": False,
        "title": title[:140],
        "matched": [],
        "reason": "",
    }
    if not enabled:
        report["reason"] = "disabled"
        return report

    patterns = list(scheme.get("random_title_patterns") or DEFAULT_RANDOM_TITLE)
    t = _n(title)
    hits: list[str] = []
    for pat in patterns:
        try:
            if re.search(pat, t, flags=re.I):
                hits.append(pat)
        except re.error:
            continue

    # 多件装 + 随机 更危险；单「aleatorio」也拦
    if hits:
        report["hold"] = True
        report["matched"] = hits[:8]
        report["multipack"] = is_multipack_text(title)
        report["reason"] = "random_assortment"
    else:
        report["reason"] = "ok"
    return report


def sku_health_check(product: dict[str, Any]) -> dict[str, Any]:
    """保存前自检：无有效 SKU / skuMap 与规格轴对不上 → 不健康。"""
    props = product.get("skuPropertyList") or []
    valid_ids: set[str] = set()
    for prop in props:
        if not isinstance(prop, dict):
            continue
        for v in prop.get("attrValueList") or []:
            if not isinstance(v, dict):
                continue
            if v.get("isDelete") in (True, "1", 1, "true", "True"):
                continue
            vid = str(v.get("attrValueId") or "")
            if vid:
                valid_ids.add(vid)

    sku_map = product.get("skuMap") or {}
    active = 0
    orphan = 0
    if isinstance(sku_map, dict):
        for key, sku in sku_map.items():
            if not isinstance(sku, dict):
                continue
            if str(sku.get("isDelete") or "0") in ("1", "true", "True"):
                continue
            parts = [p for p in str(key).split(";") if p]
            if valid_ids and parts and not all(p in valid_ids for p in parts):
                orphan += 1
                continue
            active += 1

    report: dict[str, Any] = {
        "ok": True,
        "hold": False,
        "active_skus": active,
        "orphan_skus": orphan,
        "valid_attr_ids": len(valid_ids),
        "reason": "ok",
    }
    if active <= 0:
        report["ok"] = False
        report["hold"] = True
        report["reason"] = "no_active_sku"
    elif orphan > 0 and active == 0:
        report["ok"] = False
        report["hold"] = True
        report["reason"] = "sku_map_orphan"
    return report


def merge_hold_reports(*reports: dict[str, Any]) -> dict[str, Any]:
    """合并多个门禁；任一 hold=True 则总 hold。"""
    merged: dict[str, Any] = {
        "hold": False,
        "reason": "ok",
        "reasons": [],
        "gates": {},
    }
    for r in reports:
        if not isinstance(r, dict):
            continue
        name = str(r.get("gate") or r.get("name") or len(merged["gates"]))
        merged["gates"][name] = r
        if r.get("hold"):
            merged["hold"] = True
            why = str(r.get("reason") or name)
            merged["reasons"].append(why)
    if merged["hold"]:
        merged["reason"] = "+".join(merged["reasons"])
    return merged
