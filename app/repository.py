from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from decimal import Decimal
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
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS trade_orders ("
                    "order_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, symbol TEXT NOT NULL, "
                    "side TEXT NOT NULL, quantity TEXT NOT NULL, requested_price TEXT NOT NULL, "
                    "filled_price TEXT, currency TEXT NOT NULL, status TEXT NOT NULL, fee TEXT NOT NULL, "
                    "reason TEXT, created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trade_orders_created_at "
                    "ON trade_orders(created_at DESC)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS portfolio_positions ("
                    "symbol TEXT PRIMARY KEY, name TEXT, quantity TEXT NOT NULL, "
                    "average_price TEXT NOT NULL, purchase_amount TEXT NOT NULL, "
                    "currency TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.execute("CREATE TABLE IF NOT EXISTS broker_accounts ("
                                   "account_ref TEXT PRIMARY KEY, mode TEXT NOT NULL, last_sync_at TEXT, status TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS live_orders ("
                                   "internal_order_id TEXT PRIMARY KEY, broker_order_id TEXT UNIQUE, client_order_id TEXT UNIQUE NOT NULL, "
                                   "account_ref TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL, "
                                   "requested_price TEXT, requested_quantity TEXT NOT NULL, filled_quantity TEXT NOT NULL DEFAULT '0', "
                                   "average_filled_price TEXT, remaining_quantity TEXT NOT NULL, commission TEXT NOT NULL DEFAULT '0', "
                                   "tax TEXT NOT NULL DEFAULT '0', status TEXT NOT NULL, reason TEXT, strategy TEXT, signal_id TEXT, "
                                   "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS live_executions ("
                                   "execution_id TEXT PRIMARY KEY, broker_order_id TEXT NOT NULL, quantity TEXT NOT NULL, "
                                   "price TEXT NOT NULL, commission TEXT NOT NULL DEFAULT '0', tax TEXT NOT NULL DEFAULT '0', filled_at TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS live_positions ("
                                   "account_ref TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, quantity TEXT NOT NULL, "
                                   "average_price TEXT, market_price TEXT, market_value TEXT, profit_loss TEXT, synced_at TEXT NOT NULL, "
                                   "PRIMARY KEY(account_ref, symbol))")
                connection.execute("CREATE TABLE IF NOT EXISTS account_snapshots ("
                                   "id INTEGER PRIMARY KEY AUTOINCREMENT, account_ref TEXT NOT NULL, captured_at TEXT NOT NULL, "
                                   "holdings TEXT NOT NULL, orders TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS reconciliation_events ("
                                   "id INTEGER PRIMARY KEY AUTOINCREMENT, account_ref TEXT NOT NULL, reconciled INTEGER NOT NULL, "
                                   "detail TEXT, created_at TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS risk_events ("
                                   "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, rule TEXT NOT NULL, allowed INTEGER NOT NULL, "
                                   "detail TEXT, created_at TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS trading_audit_log ("
                                   "id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
                position_columns = {row[1] for row in connection.execute("PRAGMA table_info(portfolio_positions)")}
                if 'name' not in position_columns:
                    connection.execute("ALTER TABLE portfolio_positions ADD COLUMN name TEXT")
                if 'purchase_amount' not in position_columns:
                    connection.execute("ALTER TABLE portfolio_positions ADD COLUMN purchase_amount TEXT")
                saved = connection.execute(
                    "SELECT payload FROM snapshots WHERE name = 'paper_account'"
                ).fetchone()
                if saved:
                    account = json.loads(saved[0])
                    self._replace_trade_orders(connection, account.get('orders', []))
                    self._replace_portfolio_positions(connection, account.get('positions', []))

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
                if name == 'paper_account':
                    self._replace_trade_orders(connection, payload.get('orders', []))
                    self._replace_portfolio_positions(connection, payload.get('positions', []))

    @staticmethod
    def _replace_trade_orders(connection: sqlite3.Connection, orders: list[dict[str, Any]]) -> None:
        """Keep a query-friendly order ledger in sync with the PAPER account snapshot."""
        connection.execute("DELETE FROM trade_orders")
        if not orders:
            return
        connection.executemany(
            "INSERT INTO trade_orders("
            "order_id, client_order_id, symbol, side, quantity, requested_price, filled_price, "
            "currency, status, fee, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(
                order['order_id'], order['client_order_id'], order['symbol'], order['side'],
                order['quantity'], order['requested_price'], order.get('filled_price'),
                order['currency'], order['status'], order.get('fee', '0'), order.get('reason'),
                order['created_at'],
            ) for order in orders],
        )

    @staticmethod
    def _replace_portfolio_positions(connection: sqlite3.Connection, positions: list[dict[str, Any]]) -> None:
        """Keep current PAPER holdings queryable as one database row per symbol."""
        names = dict(connection.execute("SELECT symbol, name FROM portfolio_positions WHERE name IS NOT NULL"))
        connection.execute("DELETE FROM portfolio_positions")
        if not positions:
            return
        connection.executemany(
            "INSERT INTO portfolio_positions(symbol, name, quantity, average_price, purchase_amount, currency, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            [(position['symbol'], position.get('name') or names.get(position['symbol']),
              position['quantity'], position['average_price'],
              str(Decimal(position['quantity']) * Decimal(position['average_price'])), position['currency'])
             for position in positions],
        )

    async def update_position_names(self, names: dict[str, str]) -> None:
        await asyncio.to_thread(self._update_position_names_sync, names)

    def _update_position_names_sync(self, names: dict[str, str]) -> None:
        if not names:
            return
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.executemany(
                    "UPDATE portfolio_positions SET name = ? WHERE symbol = ?",
                    [(name, symbol) for symbol, name in names.items() if name],
                )

    async def save_live_account_snapshot(self, account_ref: str, holdings: list[dict],
                                         orders: list[dict], captured_at: str,
                                         reconciled: bool = True, detail: str | None = None) -> None:
        await asyncio.to_thread(self._save_live_account_snapshot_sync, account_ref, holdings,
                                 orders, captured_at, reconciled, detail)

    def _save_live_account_snapshot_sync(self, account_ref, holdings, orders, captured_at,
                                         reconciled=True, detail=None):
        encoded_holdings = json.dumps(holdings, ensure_ascii=False, separators=(',', ':'))
        encoded_orders = json.dumps(orders, ensure_ascii=False, separators=(',', ':'))
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("INSERT INTO account_snapshots(account_ref,captured_at,holdings,orders) VALUES(?,?,?,?)",
                                   (account_ref, captured_at, encoded_holdings, encoded_orders))
                status = 'SYNCED' if reconciled else 'MISMATCH'
                connection.execute("INSERT INTO broker_accounts(account_ref,mode,last_sync_at,status) VALUES(?, 'live-readonly', ?, ?) "
                                   "ON CONFLICT(account_ref) DO UPDATE SET last_sync_at=excluded.last_sync_at,status=excluded.status",
                                   (account_ref, captured_at, status))
                if reconciled:
                    connection.execute("DELETE FROM live_positions WHERE account_ref=?", (account_ref,))
                    for item in holdings:
                        stock = item.get('stock') if isinstance(item.get('stock'), dict) else {}
                        symbol = item.get('symbol') or stock.get('symbol')
                        if not symbol:
                            continue
                        connection.execute("INSERT INTO live_positions(account_ref,symbol,name,quantity,average_price,market_price,market_value,profit_loss,synced_at) "
                                           "VALUES(?,?,?,?,?,?,?,?,?)",
                                           (account_ref, str(symbol).upper(), item.get('name') or stock.get('name'), str(item.get('quantity', '0')),
                                            str(item.get('averagePurchasePrice') or item.get('averagePrice') or item.get('average_price') or ''),
                                            str(item.get('lastPrice') or item.get('marketPrice') or item.get('market_price') or ''),
                                            str(item.get('marketValue') or item.get('market_value') or ''),
                                            str(item.get('profitLoss') or item.get('profit_loss') or ''), captured_at))
                event_detail = detail or f'holdings={len(holdings)},orders={len(orders)}'
                connection.execute("INSERT INTO reconciliation_events(account_ref,reconciled,detail,created_at) VALUES(?,?,?,?)",
                                   (account_ref, int(reconciled), event_detail, captured_at))

    async def append_reconciliation_event(self, account_ref: str, reconciled: bool,
                                          detail: str, created_at: str) -> None:
        await asyncio.to_thread(self._append_reconciliation_event_sync, account_ref, reconciled, detail, created_at)

    async def append_risk_event(self, symbol: str | None, rule: str, allowed: bool, detail: str) -> None:
        await asyncio.to_thread(self._append_risk_event_sync, symbol, rule, allowed, detail)

    def _append_risk_event_sync(self, symbol, rule, allowed, detail):
        from datetime import datetime, timezone
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO risk_events(symbol,rule,allowed,detail,created_at) VALUES(?,?,?,?,?)",
                    (symbol, rule, int(allowed), detail, datetime.now(timezone.utc).isoformat()),
                )

    def _append_reconciliation_event_sync(self, account_ref, reconciled, detail, created_at):
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("INSERT INTO reconciliation_events(account_ref,reconciled,detail,created_at) VALUES(?,?,?,?)",
                                   (account_ref, int(reconciled), detail, created_at))

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
