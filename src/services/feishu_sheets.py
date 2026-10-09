# -*- coding: utf-8 -*-
"""飞书电子表格：追加门禁未过链接。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class FeishuSheetsError(RuntimeError):
    pass


class FeishuSheets:
    def __init__(self, app_id: str, app_secret: str, spreadsheet_token: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.spreadsheet_token = spreadsheet_token
        self._token: str | None = None
        self._token_expire_at = 0.0
        self._sheet_id: str | None = None

    def _http(self, method: str, url: str, body: dict | None = None, auth: bool = True) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(1, 4):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json; charset=utf-8")
            if auth:
                req.add_header("Authorization", f"Bearer {self._access_token()}")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                try:
                    j = json.loads(raw)
                except Exception:
                    j = {"raw": raw}
                raise FeishuSheetsError(f"HTTP {e.code}: {j}") from e
            except Exception as e:
                last_err = e
                time.sleep(1.2 * attempt)
        raise FeishuSheetsError(f"request failed: {last_err}") from last_err

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token
        body = self._http(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
            auth=False,
        )
        if body.get("code") != 0:
            raise FeishuSheetsError(f"token fail: {body}")
        self._token = body["tenant_access_token"]
        self._token_expire_at = time.time() + float(body.get("expire") or 7200)
        return self._token

    def list_sheets(self) -> list[dict[str, Any]]:
        url = (
            f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/"
            f"{self.spreadsheet_token}/sheets/query"
        )
        body = self._http("GET", url)
        if body.get("code") != 0:
            # fallback v2 meta
            url2 = (
                f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/"
                f"{self.spreadsheet_token}/metainfo"
            )
            body2 = self._http("GET", url2)
            if body2.get("code") != 0:
                raise FeishuSheetsError(f"sheets meta fail: {body} / {body2}")
            sheets = (body2.get("data") or {}).get("sheets") or []
            return sheets
        return (body.get("data") or {}).get("sheets") or []

    def ensure_sheet_id(self, prefer_title: str | None = None) -> str:
        if self._sheet_id:
            return self._sheet_id
        sheets = self.list_sheets()
        if not sheets:
            raise FeishuSheetsError("spreadsheet has no sheets")
        if prefer_title:
            for s in sheets:
                title = str(s.get("title") or s.get("sheet_name") or "")
                if title == prefer_title:
                    sid = str(s.get("sheet_id") or s.get("sheetId") or "")
                    if sid:
                        self._sheet_id = sid
                        return sid
        first = sheets[0]
        sid = str(first.get("sheet_id") or first.get("sheetId") or "")
        if not sid:
            raise FeishuSheetsError(f"no sheet_id in {first}")
        self._sheet_id = sid
        return sid

    def ensure_header(self, headers: list[str]) -> None:
        sheet_id = self.ensure_sheet_id()
        rng = f"{sheet_id}!A1:{chr(ord('A') + len(headers) - 1)}1"
        url = (
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/"
            f"{self.spreadsheet_token}/values/{urllib.parse.quote(rng, safe='')}"
        )
        body = self._http("GET", url)
        if body.get("code") != 0:
            raise FeishuSheetsError(f"read header fail: {body}")
        values = ((body.get("data") or {}).get("valueRange") or {}).get("values") or []
        first = values[0] if values else []
        if first and any(str(x).strip() for x in first):
            return
        put_url = (
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/"
            f"{self.spreadsheet_token}/values"
        )
        put = self._http(
            "PUT",
            put_url,
            {"valueRange": {"range": rng, "values": [headers]}},
        )
        if put.get("code") != 0:
            raise FeishuSheetsError(f"write header fail: {put}")

    def append_rows(self, rows: list[list[Any]], col_end: str = "G") -> dict[str, Any]:
        if not rows:
            return {}
        sheet_id = self.ensure_sheet_id()
        rng = f"{sheet_id}!A:{col_end}"
        url = (
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/"
            f"{self.spreadsheet_token}/values_append?insertDataOption=INSERT_ROWS"
        )
        body = self._http(
            "POST",
            url,
            {"valueRange": {"range": rng, "values": rows}},
        )
        if body.get("code") != 0:
            raise FeishuSheetsError(f"append fail: {body}")
        return body
