"""Per-shop daily publish quota (local JSON)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


class DailyQuota:
    def __init__(self, path: Path, limit_per_shop: int = 300):
        self.path = path
        self.limit = int(limit_per_shop)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def today_key(self) -> str:
        return date.today().isoformat()

    def count(self, shop_id: int) -> int:
        data = self._load()
        day = data.get(self.today_key()) or {}
        return int(day.get(str(shop_id)) or 0)

    def remaining(self, shop_id: int) -> int:
        return max(0, self.limit - self.count(shop_id))

    def add(self, shop_id: int, n: int = 1) -> int:
        data = self._load()
        key = self.today_key()
        day = dict(data.get(key) or {})
        day[str(shop_id)] = int(day.get(str(shop_id)) or 0) + int(n)
        data[key] = day
        # keep last 14 days only
        keys = sorted(k for k in data.keys() if isinstance(data.get(k), dict))
        for old in keys[:-14]:
            data.pop(old, None)
        self._save(data)
        return int(day[str(shop_id)])

    def snapshot(self, shop_ids: list[int]) -> dict[str, int]:
        return {str(s): self.count(s) for s in shop_ids}
