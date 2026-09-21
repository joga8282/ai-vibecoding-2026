from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class SnapshotRepository:
    """Small SQLite snapshot store for the v0.1 paper ledger."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS snapshots "
                    "(name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )

    async def load(self, name: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._load_sync, name)

    def _load_sync(self, name: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT payload FROM snapshots WHERE name = ?", (name,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    async def save(self, name: str, payload: dict[str, Any]) -> None:
        await asyncio.to_thread(self._save_sync, name, payload)

    def _save_sync(self, name: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO snapshots(name, payload, updated_at) "
                    "VALUES (?, ?, datetime('now')) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at",
                    (name, encoded),
                )
