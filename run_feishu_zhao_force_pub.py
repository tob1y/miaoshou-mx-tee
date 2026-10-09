"""Force publish detail 3371025071 to 小赵1店; update Feishu."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from api.miaoshou_client import MiaoshouClient
from services.publisher import publish_details

DETAIL = 3371025071
SHOP = 18545044
RECORD = "recvuTO0k5Ttk0"


def main():
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    m = settings["miaoshou"]
    client = MiaoshouClient(m["app_key"], m["app_secret"], timeout=120, min_interval=1.5)
    pub_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    pub_cfg["shop_id"] = SHOP
    pub_cfg["shop_name"] = "小赵1店"
    pub_cfg.setdefault("anti_duplicate", {})["filter_published"] = False

    # ensure shop claimed
    client.claim_to_shops([DETAIL], [SHOP])
    time.sleep(1.5)

    pub = publish_details(
        client,
        [DETAIL],
        pub_cfg,
        dry_run=False,
        confirm=True,
        prep_before_publish=False,
    )
    print(json.dumps(pub, ensure_ascii=False, indent=2)[:2000])
    calls = pub.get("publish_calls") or []
    ok = any(c.get("ok") for c in calls) if calls else False

    f = settings["feishu"]
    tok = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                data=json.dumps({"app_id": f["app_id"], "app_secret": f["app_secret"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        ).read().decode()
    )["tenant_access_token"]
    now_ms = int(time.time() * 1000)
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{f['bitable_app_token']}"
        f"/tables/{f['bitable_table_id']}/records/{RECORD}"
    )
    body = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(
                    {"fields": {"上架状态": "是" if ok else "否", "上架时间": now_ms, "入库状态": "是", "入库时间": now_ms}}
                ).encode(),
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                method="PUT",
            )
        ).read().decode()
    )
    print("feishu", body.get("code"), body.get("msg"), "ok", ok)


if __name__ == "__main__":
    main()
