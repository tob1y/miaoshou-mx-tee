# -*- coding: utf-8 -*-
"""水洗黑命中 → 飞书电子表格追加。"""
from __future__ import annotations

from typing import Any

from services.feishu_sheets import FeishuSheets, FeishuSheetsError

HEADERS = ["标题", "店铺", "商品ID", "detailId", "货源ID", "命中原因", "gmtCreate", "编辑链接"]


def hit_to_row(h: dict[str, Any]) -> list[Any]:
    return [
        str(h.get("title") or "")[:200],
        str(h.get("shop_name") or ""),
        str(h.get("platform_item_id") or ""),
        str(h.get("detail_id") or ""),
        str(h.get("source_id") or ""),
        ",".join(h.get("reasons") or []) if isinstance(h.get("reasons"), list) else str(h.get("reasons") or ""),
        str(h.get("gmt_create") or ""),
        str(h.get("item_edit_url") or ""),
    ]


class WashedBlackFeishuSink:
    def __init__(self, app_id: str, app_secret: str, spreadsheet_token: str, log=None):
        self.sheets = FeishuSheets(app_id, app_secret, spreadsheet_token)
        self.log = log or (lambda m: None)
        self._ready = False
        self._disabled_reason = ""

    def ensure(self) -> bool:
        if self._ready:
            return True
        if self._disabled_reason:
            return False
        try:
            self.sheets.ensure_sheet_id()
            self.sheets.ensure_header(HEADERS)
            self._ready = True
            return True
        except Exception as e:
            self._disabled_reason = str(e)[:240]
            self.log(f"飞书水洗黑表不可用（将仅本地落盘）: {self._disabled_reason}")
            return False

    def append_hits(self, hits: list[dict[str, Any]]) -> int:
        if not hits:
            return 0
        if not self.ensure():
            return 0
        rows = [hit_to_row(h) for h in hits]
        try:
            self.sheets.append_rows(rows, col_end="H")
            return len(rows)
        except FeishuSheetsError as e:
            self.log(f"飞书追加失败: {e}")
            return 0
        except Exception as e:
            self.log(f"飞书追加异常: {e}")
            return 0
