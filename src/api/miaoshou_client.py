"""Miaoshou OpenAPI client — CONFIRMED signing + collect-box paths."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Any

import requests

from api import endpoints as ep


class MiaoshouError(RuntimeError):
    pass


def make_body_json(data: dict[str, Any]) -> str:
    # CONFIRMED
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def generate_sign(app_secret: str, path: str, timestamp: str, app_key: str, body_json: str) -> str:
    # Official: HMAC_SHA256(secret, secret + path + timestamp + appKey + bodyJson + secret)
    content = (
        app_secret
        + path
        + timestamp
        + app_key
        + body_json
        + app_secret
    )
    return hmac.new(
        app_secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class MiaoshouClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        base_url: str = "https://openapi-erp.91miaoshou.com",
        timeout: float = 60,
        min_interval: float = 0.8,
        max_retries: int = 4,
        sign_join: str = "concat",  # concat | ampersand
    ):
        # strip + 去掉中间误粘贴空白，避免 AppId 尾部空格导致 signInvalid
        self.app_key = "".join(str(app_key).split())
        self.app_secret = "".join(str(app_secret).split())
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.sign_join = sign_join
        self._last = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()

    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret and "YOUR_" not in self.app_key)

    def _throttle(self) -> None:
        with self._lock:
            gap = time.time() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.time()

    def _sign(self, path: str, timestamp: str, body_json: str) -> str:
        return generate_sign(self.app_secret, path, timestamp, self.app_key, body_json)

    @staticmethod
    def _is_qps_body(body: dict[str, Any]) -> bool:
        code = str(body.get("code") or "")
        msg = str(body.get("message") or "")
        blob = code + msg
        return any(
            x in blob
            for x in (
                "Qps",
                "Qpm",
                "QPS",
                "QPM",
                "accountApiQpsRateLimit",
                "频率",
                "rateLimit",
                "RateLimit",
            )
        )

    def post(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured():
            raise MiaoshouError("请先在 config/settings.yaml 填写 app_key / app_secret")
        payload = data or {}
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            ts = str(int(time.time()))
            body_json = make_body_json(payload)
            headers = {
                "x-app-key": self.app_key,
                "x-timestamp": ts,
                "x-sign": self._sign(path, ts, body_json),
                "Content-Type": "application/json",
            }
            try:
                resp = self.session.post(
                    self.base_url + path,
                    data=body_json.encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                time.sleep(min(12, 1.2 * attempt))
                continue

            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if resp.status_code in (420, 429):
                delay = float(retry_after) if retry_after else min(16, 2.5 * attempt)
                time.sleep(delay)
                continue

            try:
                body = resp.json()
            except Exception:
                body = {"http_status": resp.status_code, "text": resp.text[:500]}

            if resp.status_code >= 500:
                time.sleep(min(12, 1.2 * attempt))
                last_err = MiaoshouError(f"HTTP {resp.status_code}: {body}")
                continue

            # 业务层 QPS：HTTP 200 但 code=accountApiQpsRateLimit
            if isinstance(body, dict) and self._is_qps_body(body):
                delay = min(20, 2.0 * attempt + 1.5)
                time.sleep(delay)
                last_err = MiaoshouError(
                    f"QPS: code={body.get('code')} message={body.get('message')}"
                )
                continue

            return {"http_status": resp.status_code, "body": body}

        raise MiaoshouError(f"请求失败（已重试）: {last_err}")

    @staticmethod
    def assert_success(result: dict[str, Any], label: str = "") -> dict[str, Any]:
        http = result.get("http_status", 0)
        body = result.get("body") or {}
        # CONFIRMED: check success-ish fields flexibly
        ok = http == 200 and (
            body.get("success") is True
            or body.get("result") == "success"
            or str(body.get("code", "")).lower() in ("0", "success", "ok", "")
            or (isinstance(body.get("data"), (dict, list)) and body.get("error") is None)
        )
        # Some APIs nest result
        if not ok and http == 200 and body.get("data") is not None and not body.get("message"):
            ok = True
        if not ok:
            raise MiaoshouError(
                f"{label or 'API'}失败: HTTP={http} code={body.get('code')} message={body.get('message')} body={str(body)[:400]}"
            )
        return body

    # ---- business helpers (CONFIRMED request shapes) ----

    def get_shop_list(
        self,
        platform: str = "tiktok",
        site: str = "MX",
        page_no: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        # Official required: platform, site, pageNo, pageSize
        return self.assert_success(
            self.post(
                ep.SHOP_LIST,
                {
                    "platform": platform,
                    "site": site,
                    "pageNo": int(page_no),
                    "pageSize": min(100, int(page_size)),
                },
            ),
            "店铺列表",
        )

    def fetch_item(self, links: list[str]) -> dict[str, Any]:
        return self.assert_success(self.post(ep.FETCH_ITEM, {"collectLinks": links}), "采集商品")

    def claim_to_shops(self, detail_ids: list[int], shop_ids: list[int]) -> dict[str, Any]:
        return self.assert_success(
            self.post(ep.CLAIM_TO_SHOP, {"detailIds": detail_ids, "shopIds": shop_ids}),
            "认领店铺",
        )

    def get_shop_detail(self, detail_id: int, shop_id: int) -> tuple[str, dict[str, Any]]:
        body = self.assert_success(
            self.post(ep.GET_SHOP_DETAIL, {"detailId": int(detail_id), "shopId": int(shop_id)}),
            "读取店铺详情",
        )
        data = body.get("data") or {}
        oss = data.get("ossMd5") or ""
        product = data.get("shopCollectItemInfo") or {}
        if not oss or not isinstance(product, dict):
            raise MiaoshouError("店铺详情缺少 ossMd5 或 shopCollectItemInfo")
        return str(oss), product

    def save_shop_detail(
        self,
        oss_md5: str,
        product: dict[str, Any],
        detail_id: int,
        shop_id: int,
    ) -> dict[str, Any]:
        # CONFIRMED envelope
        p = json.loads(json.dumps(product))  # deep copy via json
        p.setdefault("deliveryOptionSetType", "default")
        return self.assert_success(
            self.post(
                ep.SAVE_SHOP_DETAIL,
                {
                    "ossMd5": oss_md5,
                    "shopCollectItemInfo": p,
                    "detailId": int(detail_id),
                    "shopId": int(shop_id),
                },
            ),
            "保存改品",
        )

    def search_tiktok_box(
        self,
        *,
        page_no: int = 1,
        page_size: int = 500,
        status: str | None = "notPublished",
        source_item_id: str | None = None,
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """List TikTok collect-box items.

        Official filter.status:
          notPublished | timingPublish | published
        """
        filt: dict[str, Any] = dict(extra_filter or {})
        if status:
            filt["status"] = status
        if source_item_id:
            filt["sourceItemIdKeyword"] = str(source_item_id)
        body: dict[str, Any] = {
            "pageNo": max(1, int(page_no)),
            "pageSize": min(500, int(page_size)),
        }
        if filt:
            body["filter"] = filt
        return self.assert_success(self.post(ep.TIKTOK_LIST, body), "查询TikTok采集箱")

    def get_shop_warehouse_list(self, shop_ids: list[int]) -> dict[str, Any]:
        return self.assert_success(
            self.post(ep.SHOP_WAREHOUSE_LIST, {"shopIds": [int(x) for x in shop_ids]}),
            "店铺仓库列表",
        )

    def get_category_metadata(
        self,
        cid: int | str,
        site: str | None = None,
        shop_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"cid": int(cid)}
        if site:
            payload["site"] = site
        if shop_ids:
            payload["shopIds"] = [int(x) for x in shop_ids]
        return self.assert_success(self.post(ep.CATEGORY_META, payload), "类目属性")

    def publish_collect_items(
        self,
        detail_ids: list[int],
        shop_ids: list[int],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建发布任务。官方必填 shopIds + detailIds；其余为可选扩展字段。"""
        payload: dict[str, Any] = {
            "shopIds": [int(x) for x in shop_ids],
            "detailIds": [int(x) for x in detail_ids],
        }
        if extra:
            # 不覆盖必填键
            for k, v in extra.items():
                if k in ("shopIds", "detailIds"):
                    continue
                payload[k] = v
        return self.assert_success(self.post(ep.PUBLISH, payload), "发布产品")

    def search_publish_tasks(
        self,
        *,
        page_no: int = 1,
        page_size: int = 20,
        shop_id: int | None = None,
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """查询发布记录 / 搬家任务列表。"""
        body: dict[str, Any] = {
            "pageNo": max(1, int(page_no)),
            "pageSize": min(100, int(page_size)),
        }
        if shop_id is not None:
            body["shopId"] = int(shop_id)
        if extra_filter:
            body["filter"] = dict(extra_filter)
        return self.assert_success(self.post(ep.PUBLISH_LIST, body), "发布记录")

