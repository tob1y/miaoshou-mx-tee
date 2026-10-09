"""Only tests Miaoshou OpenAPI signing via get_shop_list. Do not print AppSecret."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import yaml

from api.endpoints import SHOP_LIST
from api.miaoshou_client import MiaoshouClient


def main() -> int:
    settings_path = ROOT / "config" / "settings.yaml"
    with settings_path.open(encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}
    m = settings.get("miaoshou") or {}
    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=float(m.get("min_request_interval_seconds") or 0.8),
    )
    if not client.configured():
        print("FAIL: app_key/app_secret not configured")
        return 1

    print("POST", SHOP_LIST)
    print("app_key_len", len(client.app_key))
    print("app_secret_len", len(client.app_secret))
    # Official required body for get_shop_list
    m = settings.get("miaoshou") or {}
    body = {
        "platform": str(m.get("platform") or "tiktok"),
        "site": str(m.get("site") or "MX"),
        "pageNo": 1,
        "pageSize": 100,
    }
    result = client.post(SHOP_LIST, body)
    http = result.get("http_status")
    body = result.get("body") or {}
    code = body.get("code")
    message = body.get("message")
    outcome = body.get("result")

    print("http_status", http)
    print("result", outcome)
    print("code", code)
    print("message", message)
    # safe dump: never include credentials
    print("body", json.dumps(body, ensure_ascii=False)[:800])

    if code == "signInvalid":
        print("VERDICT: sign still invalid")
        return 2
    if code in ("appNoPermission", "ipNotInWhitelist"):
        print("VERDICT: sign OK; blocked by", code)
        return 0
    if outcome == "success" or str(code).lower() in ("0", "success", "ok"):
        print("VERDICT: sign + credentials + permission OK")
        return 0
    print("VERDICT: sign likely OK (not signInvalid); inspect code/message above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
