from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.models import Currency, Position


class TossRealBroker:
    """Read-only real-account adapter. Order submission stays locked until a later phase."""

    def __init__(self, client, account_seq: str, repository) -> None:
        self.client = client
        self.account_seq = account_seq
        self.repository = repository
        self.positions: dict[str, Position] = {}
        self.orders: list = []
        self.cash = {Currency.KRW: Decimal(0), Currency.USD: Decimal(0)}
        self.last_sync_at: datetime | None = None
        self.last_error: str | None = None
        self.reconciled = False

    async def restore(self) -> None:
        # LIVE state is never restored as truth; the broker account is authoritative.
        self.positions = {}
        self.orders = []

    async def get_accounts(self) -> list[dict]:
        return await self.client.accounts()

    async def get_positions(self) -> list[dict]:
        return await self.client.holdings(self.account_seq)

    async def get_buying_power(self, symbol: str | None = None) -> dict:
        return await self.client.buying_power(self.account_seq, symbol)

    async def get_sellable_quantity(self, symbol: str) -> dict:
        return await self.client.sellable_quantity(self.account_seq, symbol)

    async def list_orders(self, status: str | None = None) -> list[dict]:
        return await self.client.account_orders(self.account_seq, status)

    async def get_order(self, order_id: str) -> dict:
        return await self.client.account_order(self.account_seq, order_id)

    async def place_order(self, request: dict) -> dict:
        raise RuntimeError('LIVE 주문 전송은 아직 잠겨 있습니다. 읽기 전용 계좌 동기화만 지원합니다.')

    async def cancel_order(self, order_id: str) -> dict:
        raise RuntimeError('LIVE 주문 취소는 아직 잠겨 있습니다. 읽기 전용 계좌 동기화만 지원합니다.')

    async def reconcile(self) -> dict:
        started = datetime.now(timezone.utc)
        try:
            holdings = await self.get_positions()
            orders = await self.list_orders()
            await self.repository.save_live_account_snapshot(
                self.account_seq, holdings, orders, started.isoformat()
            )
            self.last_sync_at = started
            self.last_error = None
            self.reconciled = True
            return {'reconciled': True, 'positions': holdings, 'orders': orders,
                    'synced_at': started.isoformat(), 'mismatches': []}
        except Exception as exc:
            self.last_error = f'{type(exc).__name__}: {exc}'
            self.reconciled = False
            await self.repository.append_reconciliation_event(
                self.account_seq, False, self.last_error, started.isoformat()
            )
            raise

    def account(self, quotes: dict) -> dict:
        return {'mode': 'live-readonly', 'cash': {key.value: str(value) for key, value in self.cash.items()},
                'positions': [], 'realized_profit_loss': {'KRW': '0', 'USD': '0'},
                'unrealized_profit_loss': {'KRW': '0', 'USD': '0'},
                'total_equity': {'KRW': '0', 'USD': '0'}, 'data_status': 'read-only'}

    def performance(self, quotes: dict) -> dict:
        return {'by_currency': {}, 'orders': {'total': 0, 'filled': 0, 'rejected': 0},
                'total_fees': {'KRW': '0', 'USD': '0'}}

    async def place_market_order(self, **kwargs):
        raise RuntimeError('LIVE 주문 전송은 아직 잠겨 있습니다.')
