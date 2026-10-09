from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  detail_id INTEGER,
  shop_id INTEGER,
  status TEXT,
  preview_json TEXT,
  error TEXT,
  created_at TEXT,
  updated_at TEXT
);
"""


class RunDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        return sqlite3.connect(self.path)

    def log(self, detail_id: int, shop_id: int, status: str, preview: dict[str, Any] | None = None, error: str = "") -> str:
        rid = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs(id,detail_id,shop_id,status,preview_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    rid,
                    detail_id,
                    shop_id,
                    status,
                    json.dumps(preview or {}, ensure_ascii=False),
                    error,
                    now,
                    now,
                ),
            )
        return rid
