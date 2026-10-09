# -*- coding: utf-8 -*-
"""识图门禁：袖子必须与衣身同色纯色；异色/花袖 → 整链留库，不做删图改图。"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from typing import Any

# 进程内同图结果缓存：跨链接复用，避免同一 CDN 图重复计费
# key = 去掉 query 后的 URL（TikTok 图身份在 path，query 多为跟踪参数）
_VISION_RESULT_CACHE: dict[str, dict[str, Any]] = {}
_VISION_CACHE_LOCK = threading.Lock()
_VISION_CACHE_MAX = 4000


def vision_cache_key(image_url: str) -> str:
    u = (image_url or "").strip()
    if not u:
        return ""
    return u.split("?", 1)[0]


def clear_vision_result_cache() -> None:
    with _VISION_CACHE_LOCK:
        _VISION_RESULT_CACHE.clear()


def vision_cache_stats() -> dict[str, int]:
    with _VISION_CACHE_LOCK:
        return {"size": len(_VISION_RESULT_CACHE), "max": _VISION_CACHE_MAX}


def _cache_get(image_url: str) -> dict[str, Any] | None:
    key = vision_cache_key(image_url)
    if not key:
        return None
    with _VISION_CACHE_LOCK:
        hit = _VISION_RESULT_CACHE.get(key)
        if hit is None:
            return None
        out = dict(hit)
    out["cache_hit"] = True
    out["elapsed_s"] = 0.0
    out["prompt_tokens"] = 0
    out["completion_tokens"] = 0
    out["total_tokens"] = 0
    out["image_url"] = image_url[:200]
    return out


def _cache_put(image_url: str, result: dict[str, Any]) -> None:
    key = vision_cache_key(image_url)
    if not key or result.get("error"):
        return
    stored = {
        "raw_content": result.get("raw_content"),
        "contrast_sleeves": bool(result.get("contrast_sleeves")),
        "confident": bool(result.get("confident")),
        "reason": str(result.get("reason") or "")[:200],
        "model": result.get("model"),
        "image_detail": result.get("image_detail") or "",
    }
    with _VISION_CACHE_LOCK:
        if len(_VISION_RESULT_CACHE) >= _VISION_CACHE_MAX and key not in _VISION_RESULT_CACHE:
            # 简单淘汰：删最早插入的一半，避免无限涨
            drop = list(_VISION_RESULT_CACHE.keys())[: _VISION_CACHE_MAX // 2]
            for k in drop:
                _VISION_RESULT_CACHE.pop(k, None)
        _VISION_RESULT_CACHE[key] = stored


PROMPT = (
    "You are checking a T-shirt product photo for listing QC. "
    "Answer ONLY valid JSON with keys: contrast_sleeves (boolean), confident (boolean), reason (short English). "
    "Rule: sleeves MUST be a solid color AND the SAME fabric color as the torso/body. "
    "Set contrast_sleeves=true if ANY of these is clearly true: "
    "(1) sleeve fabric is a different color from the body (raglan/color-block/contrast sleeves); "
    "(2) sleeves are patterned, striped, color-blocked, or not a solid fill matching the body. "
    "Chest/back prints that do not change the sleeve fabric color do NOT count. "
    "Shadows, wrinkles, folds, or lighting do NOT count. "
    "If the photo does not clearly show sleeves, set contrast_sleeves=false and confident=false. "
    "If unsure, set contrast_sleeves=false and confident=false."
)


def _http_urls(vals: Any) -> list[str]:
    out: list[str] = []
    if isinstance(vals, list):
        for u in vals:
            if isinstance(u, dict):
                for k in ("url", "imgUrl", "imageUrl", "picUrl"):
                    s = str(u.get(k) or "").strip()
                    if s.startswith("http"):
                        out.append(s)
                        break
            else:
                s = str(u or "").strip()
                if s.startswith("http"):
                    out.append(s)
    elif isinstance(vals, str) and vals.strip().startswith("http"):
        out.append(vals.strip())
    return out


def collect_vision_image_urls(
    product: dict[str, Any],
    *,
    scan_main: bool = True,
    scan_options: bool = True,
    max_images: int = 20,
) -> list[str]:
    """主图 + 选项图（去重），供异色袖扫描。不做任何删改。"""
    urls: list[str] = []
    if scan_main:
        for key in ("imgUrls", "main_images", "imageList", "images"):
            urls.extend(_http_urls(product.get(key)))
        thumb = str(product.get("thumbnail") or product.get("mainImage") or "").strip()
        if thumb.startswith("http"):
            urls.append(thumb)

    if scan_options:
        for prop in product.get("skuPropertyList") or []:
            if not isinstance(prop, dict):
                continue
            for v in prop.get("attrValueList") or []:
                if not isinstance(v, dict):
                    continue
                for k in ("imgUrl", "imageUrl", "picUrl"):
                    urls.extend(_http_urls(v.get(k)))
                urls.extend(_http_urls(v.get("supplementarySkuImageUrls")))
        sku_map = product.get("skuMap") or {}
        if isinstance(sku_map, dict):
            for sk in sku_map.values():
                if not isinstance(sk, dict):
                    continue
                for k in ("imgUrl", "imageUrl", "picUrl", "skuImage"):
                    urls.extend(_http_urls(sk.get(k)))

    deduped: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
        if len(deduped) >= max(1, int(max_images or 20)):
            break
    return deduped


def _parse_json_obj(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def call_vision_contrast_sleeves(
    image_url: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: float = 45.0,
    image_detail: str | None = "low",
) -> dict[str, Any]:
    """Call OpenAI-compatible multimodal chat (DashScope / OpenAI).

    Records usage tokens + wall time for cost reports.
    OpenAI: default detail=low to avoid gpt-4o-mini high-detail token blowup.
    """
    import time as _time

    url = base_url.rstrip("/") + "/chat/completions"
    image_payload: dict[str, Any] = {"url": image_url}
    if image_detail:
        image_payload["detail"] = str(image_detail)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": image_payload},
                ],
            }
        ],
    }
    # gpt-5.x / luna 等模型不支持 temperature=0，只能用默认
    model_l = (model or "").lower()
    if not any(x in model_l for x in ("gpt-5", "o1", "o3", "o4", "luna")):
        payload["temperature"] = 0
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = _time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"vision HTTP {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"vision call failed: {e}") from e
    elapsed = round(_time.time() - t0, 3)

    choices = raw.get("choices") or []
    content = ""
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("text"):
                    parts.append(str(p["text"]))
                else:
                    parts.append(str(p))
            content = "".join(parts)
    parsed = _parse_json_obj(str(content))
    usage = raw.get("usage") or {}
    return {
        "raw_content": str(content)[:500],
        "contrast_sleeves": bool(parsed.get("contrast_sleeves")),
        "confident": bool(parsed.get("confident")),
        "reason": str(parsed.get("reason") or "")[:200],
        "model": model,
        "image_url": image_url[:200],
        "elapsed_s": elapsed,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "image_detail": image_detail or "",
    }


def should_hold_contrast_sleeves(
    product: dict[str, Any],
    vision_cfg: dict[str, Any] | None,
) -> dict[str, Any]:
    """主图+选项识图：袖子非「与衣身同色纯色」→ 整链留库。

    不做任何删图/改图/改选项。未配置、调用失败、不确定 → 不拦。
    """
    cfg = vision_cfg or {}
    report: dict[str, Any] = {
        "gate": "contrast_sleeves",
        "enabled": bool(cfg.get("enabled")) and bool(cfg.get("hold_on_contrast_sleeves", True)),
        "hold": False,
        "reason": "ok",
        "image_url": "",
        "images_scanned": 0,
        "images_total": 0,
        "vision": {},
        "hits": [],
        # 明确：异色袖只留库，不对袖子做手术
        "mutate": False,
    }
    if not report["enabled"]:
        report["reason"] = "disabled"
        return report
    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key:
        report["reason"] = "no_api_key"
        return report

    scan_main = cfg.get("scan_main_images", True) is not False
    scan_options = cfg.get("scan_option_images", True) is not False
    max_images = int(cfg.get("max_images") or 20)
    urls = collect_vision_image_urls(
        product,
        scan_main=scan_main,
        scan_options=scan_options,
        max_images=max_images,
    )
    report["images_total"] = len(urls)
    if not urls:
        report["reason"] = "no_image"
        return report
    report["image_url"] = urls[0][:200]

    provider = str(cfg.get("provider") or "dashscope").lower()
    model = str(cfg.get("model") or ("qwen-vl-plus" if provider == "dashscope" else "gpt-4o-mini"))
    if provider == "dashscope":
        base_url = str(
            cfg.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
    elif provider == "openai":
        base_url = str(cfg.get("base_url") or "https://api.openai.com/v1")
    else:
        base_url = str(cfg.get("base_url") or "")
        if not base_url:
            report["reason"] = f"unknown_provider:{provider}"
            return report

    only_confident = cfg.get("hold_only_if_confident", True) is not False
    timeout = float(cfg.get("timeout_seconds") or 45)
    use_cache = cfg.get("cache_image_results", True) is not False
    # OpenAI gpt-4o-mini 默认 low，避免高清图 token 爆炸；可在 settings 设 image_detail: high
    if "image_detail" in cfg:
        image_detail = cfg.get("image_detail")
    else:
        image_detail = "low" if provider == "openai" else None
    checked: list[dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    elapsed_total = 0.0
    cache_hits = 0

    def _finalize_metrics(rep: dict[str, Any]) -> dict[str, Any]:
        rep["prompt_tokens"] = prompt_tokens
        rep["completion_tokens"] = completion_tokens
        rep["total_tokens"] = prompt_tokens + completion_tokens
        rep["elapsed_s"] = round(elapsed_total, 3)
        rep["image_detail"] = image_detail or ""
        rep["cache_hits"] = cache_hits
        # 粗算人民币：gpt-4o-mini $0.15/$0.60 per M；其他按同价估算
        usd = prompt_tokens * 0.15 / 1_000_000 + completion_tokens * 0.60 / 1_000_000
        rep["usd"] = round(usd, 6)
        rep["cny"] = round(usd * 7.2, 5)
        return rep

    for image_url in urls:
        vision: dict[str, Any] | None = None
        if use_cache:
            vision = _cache_get(image_url)
            if vision is not None:
                cache_hits += 1
        if vision is None:
            try:
                vision = call_vision_contrast_sleeves(
                    image_url,
                    api_key=api_key,
                    model=model,
                    base_url=base_url,
                    timeout=timeout,
                    image_detail=image_detail,
                )
                vision["cache_hit"] = False
                if use_cache:
                    _cache_put(image_url, vision)
            except Exception as e:
                checked.append({"image_url": image_url[:200], "error": str(e)[:160]})
                continue

        checked.append(vision)
        prompt_tokens += int(vision.get("prompt_tokens") or 0)
        completion_tokens += int(vision.get("completion_tokens") or 0)
        elapsed_total += float(vision.get("elapsed_s") or 0)
        report["images_scanned"] = len([c for c in checked if "error" not in c])
        hit = bool(vision.get("contrast_sleeves")) and (
            bool(vision.get("confident")) or not only_confident
        )
        if hit:
            report["hold"] = True
            report["reason"] = "contrast_sleeves"
            report["vision"] = vision
            report["image_url"] = image_url[:200]
            report["hits"] = [
                {
                    "image_url": image_url[:200],
                    "reason": vision.get("reason"),
                    "confident": vision.get("confident"),
                    "prompt_tokens": vision.get("prompt_tokens"),
                    "elapsed_s": vision.get("elapsed_s"),
                    "cache_hit": bool(vision.get("cache_hit")),
                }
            ]
            report["checked"] = checked
            return _finalize_metrics(report)

    report["checked"] = checked
    ok_checks = [c for c in checked if "error" not in c]
    report["images_scanned"] = len(ok_checks)
    if not ok_checks:
        err0 = (checked[0] or {}).get("error") if checked else "unknown"
        report["reason"] = f"vision_error:{err0}"[:180]
        return _finalize_metrics(report)

    report["vision"] = ok_checks[0]
    report["reason"] = "ok"
    return _finalize_metrics(report)


# —— 只删不补：只判三件事——大致主体色 / 袖是否同色纯色 / 黑是否纯黑 ——

QC_PROMPT = (
    "You are QC for MX TikTok T-shirt product photos. "
    "Ignore chest/back prints, logos, and background; judge the garment shape and BODY fabric. "
    "Answer ONLY valid JSON with keys: "
    "body_color (pure_black|white|red|pink|blue|navy|washed_black|light_gray|dark_gray|other|unclear), "
    "contrast_sleeves (boolean), "
    "is_tank_top (boolean), "
    "confident (boolean), "
    "reason (short English). "
    "Checks: "
    "(1) Rough torso/body fabric color — pick the closest of pure_black, white, red, pink, blue, navy "
    "when the body color is roughly clear. Minor lighting/wrinkles do NOT make it unclear. "
    "(2) contrast_sleeves=true only if sleeve FABRIC is a different color from the torso OR sleeves "
    "are not a solid fill matching the body. Chest prints do NOT count. "
    "(3) Black family: use pure_black ONLY for true solid black; use washed_black / light_gray / "
    "dark_gray when fabric is clearly washed, faded, charcoal, or gray-black (NOT pure black). "
    "navy = dark blue (allowed as blue family). "
    "Use other if clearly a non-library color (green, yellow, purple, beige, etc.). "
    "Use unclear ONLY when you truly cannot tell the rough body color. "
    "(4) is_tank_top=true if the garment is a tank top / vest / sleeveless tee "
    "(no sleeves or only thin straps; Spanish chaleco/camiseta sin mangas/playera de tirantes). "
    "Regular short-sleeve T-shirts must be is_tank_top=false. "
    "Set confident=true when the rough body color choice is clear enough."
)

# 卫衣专线：底色库仍走 PASS_BODY_COLORS，由 scheme.allowed_colors 再收成黑白
HOODIE_QC_PROMPT = (
    "You are QC for MX TikTok hoodie/sweatshirt (sudadera / 卫衣) product photos. "
    "Ignore chest/back/hood prints, logos, and background; judge BODY fabric and SLEEVE fabric. "
    "Answer ONLY valid JSON with keys: "
    "body_color (pure_black|white|red|pink|blue|navy|washed_black|light_gray|dark_gray|other|unclear), "
    "contrast_sleeves (boolean), "
    "is_tank_top (boolean), "
    "confident (boolean), "
    "reason (short English). "
    "Checks: "
    "(1) Rough torso/body fabric color — pick the closest library color when roughly clear. "
    "Minor lighting/wrinkles do NOT make it unclear. "
    "(2) contrast_sleeves=true if ANY is clearly true: "
    "sleeve FABRIC differs from torso (raglan/color-block/contrast sleeves); "
    "OR sleeves have prints, patterns, stripes, graphics, or extra artwork on the sleeve fabric; "
    "OR sleeves are not a solid fill matching the body. "
    "Hood/chest/back prints that do NOT change sleeve fabric do NOT count. "
    "(3) Black family: pure_black ONLY for true solid black; washed_black / light_gray / "
    "dark_gray when washed, faded, charcoal, or gray-black. "
    "Use other for non-library colors; unclear ONLY when rough body color cannot be told. "
    "(4) is_tank_top=true if sleeveless / tank / vest (no real sleeves). "
    "Normal hoodies/sweatshirts with sleeves must be is_tank_top=false. "
    "Set confident=true when the rough body color choice is clear enough."
)

PASS_BODY_COLORS = {
    "pure_black": ("black", "Negro"),
    "white": ("white", "Blanco"),
    "red": ("red", "Rojo"),
    "pink": ("pink", "Rosa"),
    "blue": ("blue", "Azul"),
    "navy": ("navy", "Azul Marino"),
}


def _body_color_allowed(bc: str, allowed_keys: set[str]) -> bool:
    """识图 body_color 是否在 scheme.allowed_colors 内（且属 PASS 库）。"""
    if bc not in PASS_BODY_COLORS:
        return False
    key = PASS_BODY_COLORS[bc][0]
    return key in allowed_keys


def _qc_cache_get(image_url: str, *, cache_ns: str = "qc3") -> dict[str, Any] | None:
    key = f"{cache_ns}:" + vision_cache_key(image_url)
    if key.endswith(":"):
        return None
    with _VISION_CACHE_LOCK:
        hit = _VISION_RESULT_CACHE.get(key)
        if hit is None:
            return None
        out = dict(hit)
    out["cache_hit"] = True
    out["elapsed_s"] = 0.0
    out["prompt_tokens"] = 0
    out["completion_tokens"] = 0
    out["total_tokens"] = 0
    out["image_url"] = image_url[:200]
    return out


def _qc_cache_put(
    image_url: str, result: dict[str, Any], *, cache_ns: str = "qc3"
) -> None:
    key = f"{cache_ns}:" + vision_cache_key(image_url)
    if key.endswith(":") or result.get("error"):
        return
    stored = {
        "raw_content": result.get("raw_content"),
        "body_color": result.get("body_color"),
        "contrast_sleeves": bool(result.get("contrast_sleeves")),
        "is_tank_top": bool(result.get("is_tank_top")),
        "confident": bool(result.get("confident")),
        "reason": str(result.get("reason") or "")[:200],
        "model": result.get("model"),
        "image_detail": result.get("image_detail") or "",
    }
    with _VISION_CACHE_LOCK:
        if len(_VISION_RESULT_CACHE) >= _VISION_CACHE_MAX and key not in _VISION_RESULT_CACHE:
            drop = list(_VISION_RESULT_CACHE.keys())[: _VISION_CACHE_MAX // 2]
            for k in drop:
                _VISION_RESULT_CACHE.pop(k, None)
        _VISION_RESULT_CACHE[key] = stored


def call_vision_body_qc(
    image_url: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: float = 45.0,
    image_detail: str | None = "low",
    prompt: str | None = None,
) -> dict[str, Any]:
    import time as _time

    url = base_url.rstrip("/") + "/chat/completions"
    image_payload: dict[str, Any] = {"url": image_url}
    if image_detail:
        image_payload["detail"] = str(image_detail)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or QC_PROMPT},
                    {"type": "image_url", "image_url": image_payload},
                ],
            }
        ],
    }
    model_l = (model or "").lower()
    if not any(x in model_l for x in ("gpt-5", "o1", "o3", "o4", "luna")):
        payload["temperature"] = 0
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = _time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"vision HTTP {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"vision call failed: {e}") from e
    elapsed = round(_time.time() - t0, 3)
    choices = raw.get("choices") or []
    content = ""
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("text"):
                    parts.append(str(p["text"]))
                else:
                    parts.append(str(p))
            content = "".join(parts)
    parsed = _parse_json_obj(str(content))
    usage = raw.get("usage") or {}
    return {
        "raw_content": str(content)[:500],
        "body_color": str(parsed.get("body_color") or "unclear").strip().lower(),
        "contrast_sleeves": bool(parsed.get("contrast_sleeves")),
        "is_tank_top": bool(parsed.get("is_tank_top")),
        "confident": bool(parsed.get("confident")),
        "reason": str(parsed.get("reason") or "")[:200],
        "model": model,
        "image_url": image_url[:200],
        "elapsed_s": elapsed,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "image_detail": image_detail or "",
    }


def _is_tank_top(v: dict[str, Any]) -> bool:
    return bool(v.get("is_tank_top"))


def _vision_block_reason(v: dict[str, Any], *, hold_unclear: bool = True) -> str | None:
    """整链拦截：仅异色袖 / 主体色完全看不清。

    非纯黑、库外色 → 不整链杀，只删该图/该选项，并反删同色主图。
    """
    bc = str(v.get("body_color") or "")
    if v.get("contrast_sleeves"):
        return "contrast_sleeves"
    if bc in PASS_BODY_COLORS:
        return None
    if hold_unclear and bc == "unclear":
        return "vision_unclear"
    return None


def _ban_body_colors_from_label(label: str) -> set[str]:
    """选项色名若是不要的色，返回应连带删除的识图 body_color 集合。"""
    from rules.colors import _DENY_COLOR_MARKERS, _n, looks_like_style_code, match_color

    lab = (label or "").strip()
    if not lab or looks_like_style_code(lab):
        return set()
    n = _n(lab)
    bans: set[str] = set()
    if _DENY_COLOR_MARKERS.search(n):
        if re.search(
            r"lavad|washed|deslavad|decolorad|wash|水洗|gris|gray|grey|charcoal|灰|炭黑|灰黑",
            n,
            re.I,
        ):
            bans.update({"washed_black", "light_gray", "dark_gray"})
        else:
            bans.add("other")
        return bans
    key = match_color(lab)
    # 能匹配但非库内（当前别名里少见）
    if key and key not in {"black", "white", "red", "pink", "blue", "navy"}:
        # light_blue 等也当库外倾向
        if key in ("light_blue",):
            bans.add("other")
        else:
            bans.add("other")
    return bans


def _label_is_unwanted_color(label: str, allowed: set[str]) -> bool:
    from rules.colors import _DENY_COLOR_MARKERS, _n, looks_like_style_code, match_color

    lab = (label or "").strip()
    if not lab or looks_like_style_code(lab):
        return False
    if lab.lower() in ("color", "colour", "colores", "颜色"):
        return False
    n = _n(lab)
    if _DENY_COLOR_MARKERS.search(n):
        return True
    key = match_color(lab)
    if key is None:
        return False
    return key not in allowed


def _option_rows(product: dict[str, Any], max_n: int = 8) -> list[dict[str, Any]]:
    from rules.titles import is_multipack_text

    rows: list[dict[str, Any]] = []
    props = product.get("skuPropertyList") or []
    color_prop = None
    color_idx = None
    for i, prop in enumerate(props):
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("name") or prop.get("attrName") or "").lower()
        if "talla" in name or "size" in name or "尺码" in name:
            continue
        if "color" in name or "colour" in name or "颜色" in name or name in (
            "color",
            "colores",
            "colour",
            "colours",
        ):
            color_prop, color_idx = prop, i
            break
    if color_prop is None:
        for i, prop in enumerate(props):
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name") or prop.get("attrName") or "").lower()
            if "talla" in name or "size" in name or "尺码" in name:
                continue
            color_prop, color_idx = prop, i
            break

    if color_prop is None:
        return rows

    for v in color_prop.get("attrValueList") or []:
        if not isinstance(v, dict):
            continue
        if str(v.get("isDelete") or "0") in ("1", "true", "True"):
            continue
        label = str(v.get("attrValue") or v.get("value") or "")
        img = ""
        for k in ("imgUrl", "imageUrl", "picUrl", "skuImage"):
            if v.get(k):
                img = str(v.get(k))
                break
        if not img:
            extras = _http_urls(v.get("supplementarySkuImageUrls"))
            if extras:
                img = extras[0]
        rows.append(
            {
                "prop_idx": color_idx,
                "value": v,
                "label": label,
                "img_url": img,
                "multipack_name": bool(label and is_multipack_text(label)),
            }
        )
        if len(rows) >= max_n:
            break
    return rows


def apply_vision_delete_only(
    product: dict[str, Any],
    vision_cfg: dict[str, Any] | None,
    scheme: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """主图+选项识图：只删不补。

    - 异色袖 / 主图主体色完全看不清 → 整链 hold
    - 选项色名不要的色 → 删选项，并反删同色主图
    - 库外色/非纯黑主图与选项 → 只删该项；选项删色再反删主图
    - 不填充 Negro/Blanco
    """
    cfg = vision_cfg or {}
    scheme = scheme or {}
    enabled = bool(cfg.get("enabled")) and (
        bool(cfg.get("body_color_qc", True)) or bool(cfg.get("delete_only_no_fill", True))
    )
    report: dict[str, Any] = {
        "gate": "vision_delete_only",
        "enabled": enabled,
        "hold": False,
        "reason": "ok",
        "mutate": True,
        "fill_black_white": False,
        "actions": [],
        "keep_main_colors": [],
        "mapped_colors": [],
        "drop_mains": [],
        "drop_options": [],
        "images_scanned": 0,
        "images_total": 0,
        "vision": {},
        "hits": [],
        "checked": [],
    }
    if not enabled:
        report["reason"] = "disabled"
        report["mutate"] = False
        return report
    # 兼容：若显式关闭 body qc，回退旧异色袖
    if cfg.get("body_color_qc") is False and cfg.get("delete_only_no_fill") is False:
        return should_hold_contrast_sleeves(product, vision_cfg)

    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key:
        report["reason"] = "no_api_key"
        report["mutate"] = False
        return report

    provider = str(cfg.get("provider") or "dashscope").lower()
    model = str(cfg.get("model") or ("qwen-vl-plus" if provider == "dashscope" else "gpt-4o-mini"))
    if provider == "dashscope":
        base_url = str(cfg.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1")
    elif provider == "openai":
        base_url = str(cfg.get("base_url") or "https://api.openai.com/v1")
    else:
        base_url = str(cfg.get("base_url") or "")
        if not base_url:
            report["reason"] = f"unknown_provider:{provider}"
            report["mutate"] = False
            return report

    timeout = float(cfg.get("timeout_seconds") or 45)
    use_cache = cfg.get("cache_image_results", True) is not False
    hold_unclear = cfg.get("hold_on_unclear", True) is not False
    if "image_detail" in cfg:
        image_detail = cfg.get("image_detail")
    else:
        image_detail = "low" if provider == "openai" else None

    max_main = int(cfg.get("max_main_images") or 4)
    max_opt = int(cfg.get("max_option_images") or 8)
    product_type = str(
        cfg.get("product_type")
        or (scheme or {}).get("vision_product_type")
        or "tee"
    ).lower()
    is_hoodie = product_type in ("hoodie", "sweatshirt", "卫衣", "sudadera")
    qc_prompt = HOODIE_QC_PROMPT if is_hoodie else QC_PROMPT
    cache_ns = "qc_hoodie" if is_hoodie else "qc3"
    report["product_type"] = "hoodie" if is_hoodie else "tee"
    mains = collect_vision_image_urls(
        product, scan_main=True, scan_options=False, max_images=max_main
    )
    report["images_total"] = len(mains)
    if not mains:
        report["hold"] = True
        report["reason"] = "no_image"
        report["mutate"] = False
        return report

    prompt_tokens = 0
    completion_tokens = 0
    elapsed_total = 0.0
    cache_hits = 0
    checked: list[dict[str, Any]] = []

    def _call(u: str, role: str) -> dict[str, Any]:
        nonlocal prompt_tokens, completion_tokens, elapsed_total, cache_hits
        vision: dict[str, Any] | None = None
        if use_cache:
            vision = _qc_cache_get(u, cache_ns=cache_ns)
            if vision is not None:
                cache_hits += 1
        if vision is None:
            vision = call_vision_body_qc(
                u,
                api_key=api_key,
                model=model,
                base_url=base_url,
                timeout=timeout,
                image_detail=image_detail,
                prompt=qc_prompt,
            )
            vision["cache_hit"] = False
            if use_cache:
                _qc_cache_put(u, vision, cache_ns=cache_ns)
        vision = dict(vision)
        vision["role"] = role
        checked.append(vision)
        prompt_tokens += int(vision.get("prompt_tokens") or 0)
        completion_tokens += int(vision.get("completion_tokens") or 0)
        elapsed_total += float(vision.get("elapsed_s") or 0)
        return vision

    def _finish(rep: dict[str, Any]) -> dict[str, Any]:
        rep["checked"] = checked
        rep["images_scanned"] = len([c for c in checked if "error" not in c])
        rep["prompt_tokens"] = prompt_tokens
        rep["completion_tokens"] = completion_tokens
        rep["total_tokens"] = prompt_tokens + completion_tokens
        rep["elapsed_s"] = round(elapsed_total, 3)
        rep["cache_hits"] = cache_hits
        rep["image_detail"] = image_detail or ""
        usd = prompt_tokens * 0.15 / 1_000_000 + completion_tokens * 0.60 / 1_000_000
        rep["usd"] = round(usd, 6)
        rep["cny"] = round(usd * 7.2, 5)
        if checked:
            rep["vision"] = checked[0]
        return rep

    keep_main_meta: list[dict[str, Any]] = []
    keep_main_urls: list[str] = []
    drop_main_urls: list[str] = []
    ban_body_colors: set[str] = set()

    allowed_keys = {
        str(x).lower()
        for x in (
            (scheme or {}).get("allowed_colors")
            or ["black", "white", "red", "blue", "navy", "pink"]
        )
    }
    if "navy" not in allowed_keys and "blue" in allowed_keys:
        allowed_keys.add("navy")
    report["allowed_colors"] = sorted(allowed_keys)

    # —— 0) 文字：选项带「不要的色名」→ 先删选项，并记录要反删的主图色 ——
    opt_rows_all = _option_rows(product, max_n=max(max_opt, 40))
    text_drop_ids: set[str] = set()
    for o in opt_rows_all:
        label = str(o.get("label") or "")
        if not _label_is_unwanted_color(label, allowed_keys):
            continue
        bans = _ban_body_colors_from_label(label)
        ban_body_colors |= bans
        val = o["value"]
        val["isDelete"] = True
        vid = str(val.get("attrValueId") or "")
        if vid:
            text_drop_ids.add(vid)
        report["drop_options"].append({"label": label, "body_color": "text_unwanted", "bans": sorted(bans)})
        report["actions"].append(f"option_text_drop:{label}:{sorted(bans) or 'other'}")

    text_drop_n = len(report["drop_options"])
    if text_drop_n:
        # 写回：去掉已删选项（无 attrValueId 时靠 isDelete）
        prop_idx = opt_rows_all[0]["prop_idx"]
        props = list(product.get("skuPropertyList") or [])
        if 0 <= prop_idx < len(props):
            prop = dict(props[prop_idx])
            kept = []
            for v in prop.get("attrValueList") or []:
                if not isinstance(v, dict):
                    continue
                if str(v.get("attrValueId") or "") in text_drop_ids or v.get("isDelete") in (
                    True,
                    "1",
                    1,
                    "true",
                    "True",
                ):
                    continue
                kept.append(v)
            prop["attrValueList"] = kept
            props[prop_idx] = prop
            product["skuPropertyList"] = props
        report["actions"].append(f"text_dropped_options={text_drop_n}")

    hold_tank = cfg.get("hold_on_tank_top", True) is not False
    # —— 0.5) 背心：有选项图只看选项，否则看主图；任一为背心整链留库 ——
    if hold_tank:
        opt_for_tank = [
            o
            for o in _option_rows(product, max_n=max_opt)
            if str(o.get("img_url") or "").startswith("http")
        ]
        if opt_for_tank:
            tank_jobs = [(f"tank_option_{j}", str(o["img_url"]), o.get("label")) for j, o in enumerate(opt_for_tank)]
            report["actions"].append(f"tank_scan=options:{len(tank_jobs)}")
        else:
            tank_jobs = [(f"tank_main_{i}", u, None) for i, u in enumerate(mains)]
            report["actions"].append(f"tank_scan=mains:{len(tank_jobs)}")
        for role, u, label in tank_jobs:
            try:
                v = _call(u, role)
            except Exception as e:
                report["hold"] = True
                report["reason"] = f"vision_error:{e}"[:160]
                report["actions"].append(f"{role}_error")
                return _finish(report)
            if _is_tank_top(v):
                report["hold"] = True
                report["reason"] = "tank_top"
                report["actions"].append(f"{role}_block:tank_top")
                report["hits"] = [
                    {
                        "image_url": u[:200],
                        "reason": "tank_top",
                        "label": label,
                        "detail": v.get("reason"),
                    }
                ]
                report["mutate"] = False
                return _finish(report)

    # —— 1) 主图识色 ——
    for i, u in enumerate(mains):
        try:
            v = _call(u, f"main_{i}")
        except Exception as e:
            report["hold"] = True
            report["reason"] = f"vision_error:{e}"[:160]
            report["actions"].append(f"main[{i}]_error")
            return _finish(report)
        if hold_tank and _is_tank_top(v):
            report["hold"] = True
            report["reason"] = "tank_top"
            report["actions"].append(f"main[{i}]_block:tank_top")
            report["hits"] = [{"image_url": u[:200], "reason": "tank_top", "detail": v.get("reason")}]
            report["mutate"] = False
            return _finish(report)
        br = _vision_block_reason(v, hold_unclear=hold_unclear)
        if br:
            report["hold"] = True
            report["reason"] = f"main_{br}"
            report["actions"].append(f"main[{i}]_block:{br}")
            report["hits"] = [{"image_url": u[:200], "reason": br, "detail": v.get("reason")}]
            report["mutate"] = False
            return _finish(report)
        bc = str(v.get("body_color") or "")
        # 库外 / 非 scheme 允许色 / 非纯黑 / 被选项反删点名 → 删主图，不整链
        if not _body_color_allowed(bc, allowed_keys) or bc in ban_body_colors:
            drop_main_urls.append(u)
            report["drop_mains"].append({"idx": i, "body_color": bc, "banned": bc in ban_body_colors})
            report["actions"].append(f"main[{i}]_drop:{bc}")
            continue
        key, es = PASS_BODY_COLORS[bc]
        keep_main_meta.append({"idx": i, "body_color": bc, "key": key, "es": es, "url": u})
        keep_main_urls.append(u)
        report["actions"].append(f"main[{i}]_keep:{es}")

    if not keep_main_urls:
        report["hold"] = True
        report["reason"] = "main_no_library_color_left"
        report["mutate"] = False
        return _finish(report)

    def _apply_drop_mains(urls: list[str]) -> None:
        if not urls:
            return
        drop_set = set(urls)
        for key in ("imgUrls", "main_images", "imageList", "images"):
            cur = product.get(key)
            if isinstance(cur, list) and cur:
                product[key] = [
                    x
                    for x in cur
                    if str(x) not in drop_set
                    and not (isinstance(x, dict) and str(x.get("url") or "") in drop_set)
                ]

    _apply_drop_mains(drop_main_urls)
    if drop_main_urls:
        report["actions"].append(f"deleted_mains={len(drop_main_urls)}")

    report["keep_main_colors"] = [m["es"] for m in keep_main_meta]
    mapped_from_main = sorted({m["es"] for m in keep_main_meta})

    # —— 2) 选项（刷新，已去掉文字不要色）——
    opt_rows = _option_rows(product, max_n=max_opt)
    for o in opt_rows:
        if o.get("multipack_name"):
            report["hold"] = True
            report["reason"] = "multipack_option_name"
            report["actions"].append(f"option_multipack:{o.get('label')}")
            report["mutate"] = False
            return _finish(report)

    opts_with_img = [o for o in opt_rows if str(o.get("img_url") or "").startswith("http")]
    keep_opt_vals: list[dict[str, Any]] = []
    drop_opt_labels: list[str] = []
    vision_ban_extra: set[str] = set()

    if opts_with_img:
        for j, o in enumerate(opts_with_img):
            u = str(o["img_url"])
            try:
                v = _call(u, f"option_{j}")
            except Exception as e:
                report["hold"] = True
                report["reason"] = f"vision_error:{e}"[:160]
                return _finish(report)
            v["option_label"] = o.get("label")
            if hold_tank and _is_tank_top(v):
                report["hold"] = True
                report["reason"] = "tank_top"
                report["actions"].append(f"option[{j}]_block:tank_top")
                report["hits"] = [
                    {
                        "image_url": u[:200],
                        "reason": "tank_top",
                        "label": o.get("label"),
                        "detail": v.get("reason"),
                    }
                ]
                report["mutate"] = False
                return _finish(report)
            br = _vision_block_reason(v, hold_unclear=hold_unclear)
            if br == "contrast_sleeves":
                report["hold"] = True
                report["reason"] = f"option_{br}"
                report["actions"].append(f"option[{j}]_block:{br}")
                report["hits"] = [{"image_url": u[:200], "reason": br, "label": o.get("label")}]
                report["mutate"] = False
                return _finish(report)
            # unclear on option：删该选项，不整链（有其它色可留）
            bc = str(v.get("body_color") or "")
            label = str(o.get("label") or "")
            if (
                br == "vision_unclear"
                or not _body_color_allowed(bc, allowed_keys)
                or bc in ban_body_colors
            ):
                drop_opt_labels.append(label)
                if bc and bc != "unclear":
                    vision_ban_extra.add(bc)
                    ban_body_colors.add(bc)
                report["drop_options"].append({"label": label, "body_color": bc or "unclear"})
                report["actions"].append(f"option[{j}]_drop:{label}:{bc or 'unclear'}")
                val = o["value"]
                val["isDelete"] = True
                continue
            key, es = PASS_BODY_COLORS[bc]
            val = dict(o["value"])
            from rules.colors import looks_like_style_code

            if looks_like_style_code(label) or not label or label.lower() in ("color", "colour"):
                val["attrValue"] = es
            elif es.lower() not in label.lower():
                val["attrValue"] = f"{es} {label}"[:60]
            else:
                val["attrValue"] = label
            val["isDelete"] = False
            # 同色多名去重，避免「blanco重复」保存失败
            base_name = str(val["attrValue"])
            used = {str(x.get("attrValue") or "").strip().lower() for x in keep_opt_vals}
            if base_name.strip().lower() in used:
                suffix = label if label and label.strip().lower() != base_name.strip().lower() else str(j + 1)
                cand = f"{es} {suffix}"[:60]
                n = 2
                while cand.strip().lower() in used:
                    cand = f"{es} {suffix}-{n}"[:60]
                    n += 1
                val["attrValue"] = cand
            keep_opt_vals.append(val)
            report["actions"].append(f"option[{j}]_keep:{val.get('attrValue')}")

        # 反删：选项识图删掉的色 → 主图同色也删
        if vision_ban_extra:
            extra_drop = []
            new_keep_meta = []
            new_keep_urls = []
            for m in keep_main_meta:
                if m["body_color"] in ban_body_colors:
                    extra_drop.append(m["url"])
                    report["drop_mains"].append(
                        {"idx": m["idx"], "body_color": m["body_color"], "banned": True}
                    )
                    report["actions"].append(f"main[{m['idx']}]_reverse_drop:{m['body_color']}")
                else:
                    new_keep_meta.append(m)
                    new_keep_urls.append(m["url"])
            keep_main_meta = new_keep_meta
            keep_main_urls = new_keep_urls
            _apply_drop_mains(extra_drop)
            if not keep_main_urls:
                report["hold"] = True
                report["reason"] = "main_no_library_color_left"
                report["mutate"] = False
                return _finish(report)
            report["keep_main_colors"] = [m["es"] for m in keep_main_meta]
            mapped_from_main = sorted({m["es"] for m in keep_main_meta})

        if not keep_opt_vals:
            report["hold"] = True
            report["reason"] = "options_no_library_color_left"
            report["mutate"] = False
            return _finish(report)

        # 写回颜色轴（只留 keep）
        prop_idx = opts_with_img[0]["prop_idx"]
        props = list(product.get("skuPropertyList") or [])
        if 0 <= prop_idx < len(props):
            prop = dict(props[prop_idx])
            prop["attrValueList"] = keep_opt_vals
            props[prop_idx] = prop
            product["skuPropertyList"] = props
        from rules.colors import match_color, ES_NAME

        mapped = []
        for val in keep_opt_vals:
            k = match_color(str(val.get("attrValue") or ""))
            if k and ES_NAME.get(k) and ES_NAME[k] not in mapped:
                mapped.append(ES_NAME[k])
        report["mapped_colors"] = mapped or mapped_from_main
        report["actions"].append("no_fill_black_white")
        report["ban_body_colors"] = sorted(ban_body_colors)
        report["reason"] = "ok"
        return _finish(report)

    # 选项无配图：靠主图库内色写回；若文字已删光选项则 hold
    report["actions"].append("options_no_images_fallback_main")
    opt_rows = _option_rows(product, max_n=max_opt)
    if opt_rows_all and not opt_rows and text_drop_n:
        report["hold"] = True
        report["reason"] = "options_no_library_color_left"
        report["mutate"] = False
        return _finish(report)

    unique_es = []
    for m in keep_main_meta:
        if m["es"] not in unique_es:
            unique_es.append(m["es"])
    new_vals: list[dict[str, Any]] = []
    if opt_rows:
        prop_idx = opt_rows[0]["prop_idx"]
        base = opt_rows[0]["value"] if opt_rows else {}
        for i, es in enumerate(unique_es):
            item = dict(base) if isinstance(base, dict) else {}
            item["attrValue"] = es
            item["isDelete"] = False
            if i > 0:
                item["attrValueId"] = str(item.get("attrValueId") or f"vision-{i}")
            new_vals.append(item)
        props = list(product.get("skuPropertyList") or [])
        if 0 <= prop_idx < len(props):
            prop = dict(props[prop_idx])
            prop["attrValueList"] = new_vals
            props[prop_idx] = prop
            product["skuPropertyList"] = props
    else:
        product["skuPropertyList"] = list(product.get("skuPropertyList") or []) + [
            {
                "name": "Color",
                "attrValueList": [
                    {"attrValue": es, "attrValueId": f"vision-{i}", "isDelete": False}
                    for i, es in enumerate(unique_es)
                ],
            }
        ]
        report["actions"].append("created_color_axis_from_main")

    report["mapped_colors"] = unique_es
    report["actions"].append("no_fill_black_white")
    report["ban_body_colors"] = sorted(ban_body_colors)
    report["reason"] = "ok"
    return _finish(report)
