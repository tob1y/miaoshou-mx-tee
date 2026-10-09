from __future__ import annotations

import re
from typing import Any

PROMO = re.compile(
    r"(?i)\b(oferta|liquidaci[oó]n|descuento|env[ií]o\s+gratis|servicio\s+al\s+cliente|"
    r"\d+%\s*off|promo(ci[oó]n)?|free\s+shipping)\b"
)

# 原标题里的重量 → 删除，不回填 200g
WEIGHT = re.compile(
    r"(?i)(?:,\s*)?(?:\b\d+(?:[.,]\d+)?\s*(?:g|gr|gramos?|kg|kgs?)\b|"
    r"\b\d+\s*gramos?\b)(?=\s|,|\.|$)"
)

# 原标题里的尺码 → 删除，不回填 S-3XL
SIZE = re.compile(
    r"(?i)(?:,\s*)?(?:"
    r"\btallas?\b\s*(?:desde\s+)?(?:xs|s|m|l|xl|xxl|xxxl|2xl|3xl|4xl|5xl|6xl)"
    r"(?:\s*[-–~/a]\s*(?:xs|s|m|l|xl|xxl|xxxl|2xl|3xl|4xl|5xl|6xl))?|"
    r"\btalla\b\s*(?:xs|s|m|l|xl|xxl|xxxl|2xl|3xl|4xl|5xl|6xl|unica|única|unitalla)|"
    r"\bunitalla\b|\btalla\s*[uú]nica\b|\bonesize\b|\bone[\s-]?size\b|"
    r"\bdisponibles?\s+en\s+tallas?\b[^,.]*|"
    r"\bs\s*[-–~]\s*3xl\b|\bs\s*a\s*3xl\b|\bs\s*[-–~]\s*xxl\b"
    r")(?=\s|,|\.|$)"
)

# 我们旧模板残留
OUR_FORCE = re.compile(
    r"(?i),?\s*(?:200\s*g|100%\s*algod[oó]n|tallas\s*s\s*[-–~]\s*3xl|1\s*pieza)\b"
)

# 随机发货话术（改标题时剥掉；门禁另拦随机装）
RANDOM_WORDS = re.compile(
    r"(?i)(?:,?\s*)?(?:"
    r"\baleatori[oa]s?\b|"
    r"\bal\s+azar\b|"
    r"\brandom\b|"
    r"\bsorpresa\b|"
    r"\bpack\s+sorpresa\b"
    r")"
)

# 2 件及以上才算多件装（不要误伤 1pc / 1 pieza）
_N = r"(?:[2-9]|1[0-2])"
MULTIPACK = re.compile(
    r"(?i)(?:,?\s*)?(?:"
    r"\bsets?\s+de\s+" + _N + r"\b|"
    r"\bpack(?:s)?\s+(?:aleatorio\s+)?(?:de\s+)?" + _N + r"(?:\s*piezas?)?\b|"
    r"\bpaquetes?\s+de\s+" + _N + r"(?:\s*piezas?)?\b|"
    r"\bkits?\s+de\s+" + _N + r"\b|"
    r"\blote\s+de\s+" + _N + r"\b|"
    r"\b" + _N + r"\s*[-–]?\s*piezas?\b|"
    r"\b" + _N + r"\s*pcs?\b|"
    r"\b" + _N + r"\s*unidades?\b|"
    # 中文件装：不用 \\b，避免「2件装短袖」邻接汉字时漏拦
    r"(?<![0-9])" + _N + r"\s*件装|"
    r"[二三四五六七八九十两]\s*件装|"
    r"\bpack\s*x\s*" + _N + r"\b|"
    r"\bx\s*" + _N + r"\s*piezas?\b|"
    r"\b" + _N + r"\s*playeras\b|"
    r"\b" + _N + r"\s*camisetas\b"
    r")"
)


def is_multipack_text(text: str) -> bool:
    return bool(MULTIPACK.search(text or ""))


def strip_multipack_text(text: str) -> str:
    t = MULTIPACK.sub("", text or "")
    t = re.sub(r"\s*,\s*,+", ", ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")
    return t


def build_title(original: str, retained_color_keys: list[str], scheme: dict[str, Any]) -> str:
    """尽量保留原标题电商感：只去掉尺寸/重量/促销/多件装，其余多留。"""
    max_chars = int(scheme.get("title_max_chars") or 200)
    min_chars = int(scheme.get("title_min_chars") or 25)
    # unused for now — keep signature compatible
    _ = retained_color_keys

    t = re.sub(r"\s+", " ", (original or "").strip())
    t = PROMO.sub("", t)
    t = WEIGHT.sub("", t)
    t = SIZE.sub("", t)
    t = MULTIPACK.sub("", t)
    t = RANDOM_WORDS.sub("", t)
    t = OUR_FORCE.sub("", t)
    t = re.sub(r"(?i)\b1\s*pc\b", "", t)
    t = re.sub(r"\s*,\s*,+", ", ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")

    # 若已是我们旧强模板，尽量还原成「品类 + 设计」可读句，但仍偏保留
    if re.match(r"(?i)^playera\s+(negra|blanca|unisex)\b", t) and " con " in t.lower():
        # 去掉模板壳里强制加的后半属性堆砌可保留；不强制重写
        pass

    if not t:
        t = "Playera estampada casual unisex"
    if t:
        t = t[0].upper() + t[1:]

    if len(t) > max_chars:
        t = t[: max_chars - 1].rstrip(" ,.-") + "."
    if len(t) < min_chars:
        t = (t + " Moda urbana casual.").strip()
        if len(t) < min_chars:
            t = t.ljust(min_chars, ".")
    return t


def apply_title(product: dict[str, Any], scheme: dict[str, Any], retained_keys: list[str]) -> str:
    # 优先货源原标题，避免叠在已改模板上；没有 oriTitle 就用当前 title
    old = str(product.get("oriTitle") or product.get("title") or "")
    new = build_title(old, retained_keys, scheme)
    product["title"] = new
    return new
