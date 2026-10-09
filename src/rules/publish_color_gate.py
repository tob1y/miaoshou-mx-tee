"""上架门禁：标题无具体颜色 且 规格选项也无颜色 → 留采集箱、不上架。"""
from __future__ import annotations

import re
from typing import Any

from rules.colors import COLOR_ALIASES, ES_NAME, looks_like_style_code, match_color


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def title_has_concrete_color(title: str) -> tuple[bool, list[str]]:
    """标题里是否出现可识别颜色词（黑/白/红… 及西语 Negro/Blanco 等）。"""
    t = _n(title)
    if not t:
        return False, []
    hits: list[str] = []
    # 收集所有别名，长的优先，避免 "azul" 误伤已匹配 "azul marino" 的重复统计即可
    aliases: list[tuple[str, str]] = []
    for key, als in COLOR_ALIASES.items():
        for a in als + [ES_NAME.get(key, key)]:
            aliases.append((_n(a), key))
    aliases.sort(key=lambda x: -len(x[0]))
    seen_keys: set[str] = set()
    for an, key in aliases:
        if len(an) < 2:
            continue
        # 词边界近似：前后非字母数字
        if re.search(rf"(?<![a-z0-9]){re.escape(an)}(?![a-z0-9])", t):
            if key not in seen_keys:
                hits.append(key)
                seen_keys.add(key)
            continue
        # 中文无空格：直接包含
        if any("\u4e00" <= c <= "\u9fff" for c in an) and an in t:
            if key not in seen_keys:
                hits.append(key)
                seen_keys.add(key)
    return bool(hits), hits


def options_have_concrete_color(product: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """规格选项里是否有可识别的颜色销售属性取值。"""
    info: dict[str, Any] = {"axis": None, "labels": [], "recognizable": [], "style_codes": []}
    props = product.get("skuPropertyList") or []
    color_prop = None
    for prop in props:
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("name") or prop.get("attrName") or "")
        low = name.lower()
        if "color" in low or "colour" in low or "颜色" in name or "colour" in low or "color" in low:
            color_prop = prop
            info["axis"] = name
            break
        # 西语常见：Color / Colores
        if low in ("color", "colores", "colour", "colours"):
            color_prop = prop
            info["axis"] = name
            break

    labels: list[str] = []
    scan_props = [color_prop] if color_prop is not None else list(props)
    if color_prop is None:
        info["reason"] = "no_color_axis"
        # 仍扫其它轴：有的店把 Negro/Blanco 放在 Style / Tipo 里
        scan_props = [p for p in props if isinstance(p, dict)]

    for prop in scan_props:
        if not isinstance(prop, dict):
            continue
        for v in prop.get("attrValueList") or []:
            if isinstance(v, dict):
                if v.get("isDelete") in (True, "1", 1, "true", "True"):
                    continue
                labels.append(str(v.get("attrValue") or v.get("value") or ""))
            else:
                labels.append(str(v))
    labels = [x for x in labels if x.strip()]
    info["labels"] = labels[:40]
    if not labels:
        if color_prop is None:
            info["reason"] = "no_color_axis"
        else:
            info["reason"] = "color_axis_empty"
        return False, info

    recognizable = [lb for lb in labels if match_color(lb)]
    style_codes = [lb for lb in labels if looks_like_style_code(lb) and not match_color(lb)]
    info["recognizable"] = recognizable
    info["style_codes"] = style_codes

    if recognizable:
        info["reason"] = "has_recognizable_colors"
        if color_prop is None:
            info["axis"] = "non_color_axis_embedded"
        return True, info

    # 全是款号 / 无法识别 → 视为「选项没有颜色」
    info["reason"] = "only_style_codes_or_unrecognized" if color_prop is not None else "no_color_axis"
    return False, info

def should_hold_without_publish(
    product: dict[str, Any],
    scheme: dict[str, Any] | None = None,
    *,
    original_title: str | None = None,
) -> dict[str, Any]:
    """标题无具体颜色 且 选项也无颜色 → hold=True（留库不上架）。

    scheme.hold_without_color: 默认 True；设 False 关闭此门禁。
    """
    scheme = scheme or {}
    enabled = scheme.get("hold_without_color", True)
    title = original_title if original_title is not None else str(product.get("title") or "")
    report: dict[str, Any] = {
        "enabled": bool(enabled),
        "hold": False,
        "title": title[:120],
        "title_has_color": False,
        "title_colors": [],
        "options_have_color": False,
        "options": {},
        "reason": "",
    }
    if not enabled:
        report["reason"] = "disabled"
        return report

    title_ok, title_colors = title_has_concrete_color(title)
    opt_ok, opt_info = options_have_concrete_color(product)
    report["title_has_color"] = title_ok
    report["title_colors"] = title_colors
    report["options_have_color"] = opt_ok
    report["options"] = opt_info

    if (not title_ok) and (not opt_ok):
        report["hold"] = True
        report["reason"] = "title_and_options_no_concrete_color"
    else:
        report["hold"] = False
        report["reason"] = "ok_has_color_signal"
    return report
