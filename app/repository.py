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
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS signal_observations ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id TEXT NOT NULL, "
                    "observed_at TEXT NOT NULL, source TEXT NOT NULL, symbol TEXT NOT NULL, "
                    "name TEXT, price TEXT, ma20 TEXT, ma60 TEXT, bollinger_lower TEXT, "
                    "bollinger_upper TEXT, band_distance_percent TEXT, conditions_passed INTEGER, "
                    "conditions_total INTEGER, eligible INTEGER NOT NULL, reasons TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_signal_observations_time "
                    "ON signal_observations(observed_at DESC)"
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

    async def append_signal_observations(self, rows: list[dict[str, Any]]) -> int:
        return await asyncio.to_thread(self._append_signal_observations_sync, rows)

    def _append_signal_observations_sync(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        values = [(
            row['scan_id'], row['observed_at'], row['source'], row['symbol'], row.get('name'),
            row.get('price'), row.get('ma20'), row.get('ma60'), row.get('bollinger_lower'),
            row.get('bollinger_upper'), row.get('band_distance_percent'),
            row.get('conditions_passed'), row.get('conditions_total'), int(bool(row.get('eligible'))),
            json.dumps(row.get('reasons', []), ensure_ascii=False),
        ) for row in rows]
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.executemany(
                    "INSERT INTO signal_observations("
                    "scan_id, observed_at, source, symbol, name, price, ma20, ma60, "
                    "bollinger_lower, bollinger_upper, band_distance_percent, "
                    "conditions_passed, conditions_total, eligible, reasons) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values,
                )
        return len(values)

    async def signal_observation_count(self) -> int:
        return await asyncio.to_thread(self._signal_observation_count_sync)

    def _signal_observation_count_sync(self) -> int:
        with closing(sqlite3.connect(self.path)) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0])

    async def recent_signal_observations(self, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._recent_signal_observations_sync, limit)

    def _recent_signal_observations_sync(self, limit: int) -> list[dict[str, Any]]:
        columns = ('id', 'scan_id', 'observed_at', 'source', 'symbol', 'name', 'price', 'ma20',
                   'ma60', 'bollinger_lower', 'bollinger_upper', 'band_distance_percent',
                   'conditions_passed', 'conditions_total', 'eligible', 'reasons')
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT " + ",".join(columns) + " FROM signal_observations "
                "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        return [{**dict(zip(columns, row)), 'eligible': bool(row[14]),
                 'reasons': json.loads(row[15])} for row in rows]
