"""Feishu bitable helpers for 托比改品台值班."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class FeishuError(RuntimeError):
    pass


class FeishuBitable:
    def __init__(self, app_id: str, app_secret: str, app_token: str, table_id: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.table_id = table_id
        self._token: str | None = None
        self._token_expire_at = 0.0

    def _http(self, method: str, url: str, body: dict | None = None, auth: bool = True) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        last_err: Exception | None = None
        for attempt in range(1, 4):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json; charset=utf-8")
            if auth:
                req.add_header("Authorization", f"Bearer {self._access_token()}")
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                try:
                    j = json.loads(raw)
                except Exception:
                    j = {"raw": raw}
                raise FeishuError(f"HTTP {e.code}: {j}") from e
            except Exception as e:
                last_err = e
                time.sleep(1.5 * attempt)
        raise FeishuError(f"request failed after retries: {last_err}") from last_err

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
            raise FeishuError(f"token fail: {body}")
        self._token = body["tenant_access_token"]
        self._token_expire_at = time.time() + float(body.get("expire") or 7200)
        return self._token

    def list_records(self, page_size: int = 100) -> list[dict[str, Any]]:
        import urllib.parse

        items: list[dict[str, Any]] = []
        page_token = None
        page_no = 0
        while True:
            page_no += 1
            url = (
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.app_token}"
                f"/tables/{self.table_id}/records?page_size={min(500, max(1, int(page_size)))}"
                f"&automatic_fields=true"
            )
            if page_token:
                url += f"&page_token={urllib.parse.quote(str(page_token), safe='')}"
            body = None
            last_err: Exception | None = None
            for attempt in range(1, 4):
                try:
                    body = self._http("GET", url)
                    if body.get("code") == 0:
                        break
                    # 1254002 等：短暂等待后重试同页
                    last_err = FeishuError(f"list records fail: {body}")
                    time.sleep(1.5 * attempt)
                except Exception as e:
                    last_err = e
                    time.sleep(1.5 * attempt)
            if not body or body.get("code") != 0:
                raise FeishuError(f"list records fail page={page_no}: {last_err or body}")
            data = body.get("data") or {}
            batch = data.get("items") or []
            items.extend(batch)
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
            time.sleep(0.25)
        return items

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records/{record_id}"
        )
        body = self._http("PUT", url, {"fields": fields})
        if body.get("code") != 0:
            raise FeishuError(f"update fail {record_id}: {body}")
        return body

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records"
        )
        body = self._http("POST", url, {"fields": fields})
        if body.get("code") != 0:
            raise FeishuError(f"create fail: {body}")
        return body

    def with_table(self, table_id: str) -> "FeishuBitable":
        """Same app credentials, different table."""
        other = FeishuBitable(self.app_id, self.app_secret, self.app_token, table_id)
        other._token = self._token
        other._token_expire_at = self._token_expire_at
        return other
