"""Resolve TikTok short / share links to source product id."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

_PDP_RE = re.compile(r"/(?:pdp|product|view/product)/(\d{12,22})(?:/|$|\?)", re.I)


def source_item_id_from_url(url: str) -> str:
    m = _PDP_RE.search(url or "")
    return m.group(1) if m else ""


def resolve_tiktok_link(link: str, timeout: float = 18.0) -> tuple[str, str]:
    """Return (source_item_id, collect_url).

    Bugfix: vt.tiktok.com short links often 504 on Miaoshou fetch_item;
    always prefer https://shop.tiktok.com/mx/pdp/{id}.
    """
    import time as _time

    link = (link or "").strip()
    if not link:
        return "", ""
    sid = source_item_id_from_url(link)
    final = link
    if not sid:
        last_err: Exception | None = None
        # 死链别拖：最多 2 次，超时偏短
        for attempt in range(1, 3):
            try:
                r = requests.get(
                    link,
                    timeout=timeout,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                final = r.url or link
                sid = source_item_id_from_url(final)
                if not sid:
                    # query params fallback
                    q = urlparse(final).query
                    for part in q.split("&"):
                        if "product_id=" in part or "item_id=" in part:
                            sid = part.split("=", 1)[-1]
                            break
                break
            except Exception as e:
                last_err = e
                _time.sleep(0.8 * attempt)
        if not sid and last_err:
            # 让上层立刻飞书写否
            raise RuntimeError(f"{type(last_err).__name__}: {last_err}") from last_err
    if not sid:
        return "", final
    collect = f"https://shop.tiktok.com/mx/pdp/{sid}"
    return sid, collect


def extract_url_field(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return str(val.get("link") or val.get("text") or "").strip()
    if isinstance(val, list) and val:
        return extract_url_field(val[0])
    return str(val).strip()
