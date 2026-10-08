from __future__ import annotations

import logging
import secrets
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote as url_quote
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app.config import Settings
from app.dashboard_auth import COOKIE_NAME, SESSION_SECONDS, LocalDashboardSessions, is_local_dashboard_request
from app.engine import TradingEngine
from app.models import Currency, OrderStatus, Quote, Side, ThresholdStrategy, serialize, utc_now
from app.paper import PaperBroker
from app.brokers.toss_real import TossRealBroker
from app.repository import SnapshotRepository
from app.recommendation_service import build_recommendations
from app.swing_trader import SwingTrader
from app.toss import TossCandleRateLimitError, TossMarketClient
from app.weekly_report import WeeklyReportService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]


class QuoteInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    price: PositiveDecimal
    currency: Currency


class StrategyInput(BaseModel):
    strategy_id: str | None = Field(default=None, max_length=80)
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    currency: Currency
    quantity: PositiveDecimal
    buy_below: PositiveDecimal
    sell_above: PositiveDecimal
    enabled: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self) -> "StrategyInput":
        if self.buy_below >= self.sell_above:
            raise ValueError("buy_below는 sell_above보다 작아야 합니다.")
        return self


class PaperResetInput(BaseModel):
    cash_krw: NonNegativeDecimal = Decimal("10000000")
    cash_usd: NonNegativeDecimal = Decimal("10000")


class InvestmentRatioInput(BaseModel):
    ratio_percent: Decimal = Field(ge=1, le=100)


class UnsubmittedOrderReviewInput(BaseModel):
    client_order_id: str = Field(min_length=1, max_length=100)
    confirm_no_broker_order: bool = False


class LiveLimitOrderTestInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    side: Side
    quantity: Literal[1]
    limit_price: PositiveDecimal
    confirm_real_order: Literal[True]


class LiveTradeInput(BaseModel):
    confirm_real_order: Literal[True]


class ManualBuyInput(BaseModel):
    ratio_percent: Decimal | None = Field(default=None, ge=1, le=100)


class LiveBuyInput(LiveTradeInput, ManualBuyInput):
    pass


def get_engine(request: Request) -> TradingEngine:
    return request.app.state.engine


def require_live_access(request: Request) -> TossRealBroker:
    settings = request.app.state.settings
    if settings.mode != 'live' or not getattr(request.app.state, 'live_broker', None):
        raise HTTPException(status_code=409, detail='LIVE 모드가 아닙니다.')
    if settings.api_access_token:
        header_valid = secrets.compare_digest(request.headers.get('X-API-Token', ''), settings.api_access_token)
        sessions = getattr(request.app.state, 'dashboard_sessions', None)
        if not header_valid and not (sessions and sessions.allows(request, settings.api_access_token)):
            raise HTTPException(status_code=401, detail='LIVE API 인증이 필요합니다.')
    return request.app.state.live_broker


def create_app(settings: Settings | None = None) -> FastAPI:
    selected_settings = settings or Settings.from_env()
    trade_ratio_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal selected_settings
        repository = SnapshotRepository(selected_settings.database_path)
        await repository.initialize()
        trading_preferences = await repository.load("trading_preferences") or {}
        saved_ratio = trading_preferences.get("recommended_trade_ratio")
        if saved_ratio is not None:
            try:
                ratio = Decimal(str(saved_ratio))
                maximum = selected_settings.live_auto_ratio_limit if selected_settings.mode == 'live' else Decimal('.5')
                if ratio.is_finite() and Decimal('.01') <= ratio <= maximum:
                    selected_settings = replace(selected_settings, recommended_trade_ratio=ratio)
            except (ArithmeticError, ValueError, TypeError):
                logging.getLogger(__name__).warning('Ignoring invalid saved investment ratio.')
        if selected_settings.mode == 'live':
            selected_settings = replace(selected_settings, recommended_trade_ratio=min(
                selected_settings.recommended_trade_ratio, selected_settings.live_auto_ratio_limit))
        manual_maximum = selected_settings.live_manual_ratio_limit if selected_settings.mode == 'live' else Decimal(1)
        try:
            manual_ratio = Decimal(str(trading_preferences.get('manual_trade_ratio', selected_settings.manual_trade_ratio)))
            if not manual_ratio.is_finite() or not Decimal('.01') <= manual_ratio <= manual_maximum:
                manual_ratio = min(selected_settings.manual_trade_ratio, manual_maximum)
        except (ArithmeticError, ValueError, TypeError):
            manual_ratio = min(selected_settings.manual_trade_ratio, manual_maximum)
        selected_settings = replace(selected_settings, manual_trade_ratio=manual_ratio)
        toss_client = None
        if (
            selected_settings.toss_market_data_enabled
            and selected_settings.toss_client_id
            and selected_settings.toss_client_secret
        ):
            toss_client = TossMarketClient(
                selected_settings.toss_client_id, selected_settings.toss_client_secret
            )
        if selected_settings.mode == 'live':
            if not toss_client:
                raise RuntimeError('LIVE 읽기 전용 모드에는 토스 API 인증정보가 필요합니다.')
            broker = TossRealBroker(toss_client, selected_settings.toss_account_seq, repository, selected_settings)
        else:
            broker = PaperBroker(selected_settings, repository)
        await broker.restore()
        engine = TradingEngine(selected_settings, broker, repository, toss_client)
        await engine.restore()
        app.state.settings = selected_settings
        app.state.repository = repository
        app.state.broker = broker
        app.state.engine = engine
        app.state.weekly_report = WeeklyReportService()
        app.state.live_broker = broker if selected_settings.mode == 'live' else None
        if selected_settings.mode == 'live' and selected_settings.live_trading_enabled:
            try:
                result = await broker.reconcile()
                if not result['reconciled']:
                    logging.getLogger(__name__).error(
                        "LIVE startup reconciliation did not complete; account actions remain unavailable."
                    )
            except Exception as exc:
                # A temporary broker/network outage must not take down the dashboard.
                # No account values are considered current and LIVE remains disarmed.
                broker.reconciled = False
                broker.last_error = f'{type(exc).__name__}: account sync unavailable'
                if broker.risk:
                    broker.risk.armed = False
                    broker.risk.reconciled = False
                logging.getLogger(__name__).warning(
                    "LIVE startup account sync unavailable (%s); dashboard will start read-only.",
                    type(exc).__name__,
                )
        if selected_settings.mode == 'paper' or selected_settings.live_trading_enabled:
            engine.automation = SwingTrader(engine, lambda: build_recommendations(engine, app.state.weekly_report.cached_direction()))
            await engine.automation.restore()
        if selected_settings.mode in {'paper', 'live'} and toss_client and broker.positions:
            try:
                stocks = await asyncio.wait_for(toss_client.stocks_info(sorted(broker.positions)), timeout=10)
                await repository.update_position_names({stock['symbol']: stock.get('name') for stock in stocks})
            except Exception as exc:
                logging.getLogger(__name__).warning("Position name lookup failed: %s", exc)
        if selected_settings.auto_start and selected_settings.mode == 'paper':
            await engine.start()
        yield
        await engine.stop()

    app = FastAPI(
        title=selected_settings.app_name,
        version=selected_settings.version,
        description=(
            "PAPER 자동매매와 토스 LIVE 자동매매를 지원합니다. "
            "LIVE는 별도 설정, 계좌 재조정, API 인증 및 수동 무장 후에만 주문합니다."
        ),
        lifespan=lifespan,
    )
    static_dir = Path(__file__).parent / "static"
    app.state.dashboard_sessions = LocalDashboardSessions()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.post('/api/v1/auth/local-session')
    async def local_dashboard_session(request: Request) -> JSONResponse:
        if (request.headers.get('X-Dashboard-Request') != '1'
                or not is_local_dashboard_request(request, require_origin=True)):
            raise HTTPException(status_code=403, detail='현재 PC의 localhost 대시보드에서만 자동 인증할 수 있습니다.')
        settings = request.app.state.settings
        if settings.mode != 'live' or not settings.api_access_token:
            raise HTTPException(status_code=409, detail='LIVE 대시보드 인증 설정을 확인하세요.')
        session = request.app.state.dashboard_sessions.issue(request, settings.api_access_token)
        response = JSONResponse({'authenticated': True}, headers={'Cache-Control': 'no-store'})
        response.set_cookie(COOKIE_NAME, session, max_age=SESSION_SECONDS, path='/api/v1',
                            httponly=True, samesite='strict', secure=request.url.scheme == 'https')
        return response

    @app.get("/health/live")
    async def health_live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request) -> dict:
        engine = get_engine(request)
        return {
            "status": "ready",
            "mode": selected_settings.mode,
            "market_data": "toss" if engine.toss_client else "manual",
        }

    @app.get("/api/v1/system/status")
    async def system_status(request: Request) -> dict:
        return get_engine(request).status()

    @app.get('/api/v1/live/status')
    async def live_status(request: Request) -> dict:
        broker = require_live_access(request)
        return {'mode': 'live' if broker.settings and broker.settings.live_trading_enabled else 'live-readonly',
                'orders_enabled': bool(broker.risk and broker.settings and broker.settings.live_trading_enabled),
                'armed': bool(broker.risk and broker.risk.armed), 'reconciled': broker.reconciled,
                'last_sync_at': broker.last_sync_at.isoformat() if broker.last_sync_at else None,
                'last_error': broker.last_error, 'last_order_error': getattr(broker, 'last_order_error', None),
                'unresolved_order_count': sum(
                    item.get('status') in {'SUBMITTING', 'UNKNOWN', 'ACCEPTED', 'CANCEL_REQUESTED'}
                    for item in getattr(broker, 'order_journal', {}).values())}

    @app.post('/api/v1/live/arm')
    async def live_arm(request: Request) -> dict:
        broker = require_live_access(request)
        try:
            result = await broker.reconcile()
            if not result['reconciled']:
                raise RuntimeError('계좌 재조정 불일치가 있어 LIVE 무장을 거부했습니다.')
            broker.arm()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "LIVE arming failed during account sync (%s).", type(exc).__name__
            )
            raise HTTPException(
                status_code=502,
                detail=f'LIVE 계좌 동기화에 실패했습니다: {type(exc).__name__}',
            ) from exc
        return {'armed': True, 'reconciled': broker.reconciled}

    @app.post('/api/v1/live/disarm')
    async def live_disarm(request: Request) -> dict:
        broker = require_live_access(request)
        broker.disarm()
        await get_engine(request).stop()
        return {'armed': False, 'running': False}

    @app.post('/api/v1/live/reconcile')
    async def live_reconcile(request: Request) -> dict:
        broker = require_live_access(request)
        try:
            return await broker.reconcile()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'실계좌 동기화 실패: {exc}') from exc

    @app.get('/api/v1/live/accounts')
    async def live_accounts(request: Request) -> dict:
        return {'accounts': await require_live_access(request).get_accounts()}

    @app.get('/api/v1/live/order-review')
    async def live_order_review(request: Request) -> dict:
        return {'orders': require_live_access(request).unresolved_orders()}

    @app.post('/api/v1/live/order-review/confirm-unsubmitted')
    async def confirm_unsubmitted_order(payload: UnsubmittedOrderReviewInput, request: Request) -> dict:
        broker = require_live_access(request)
        if get_engine(request).running:
            raise HTTPException(status_code=409, detail='자동매매를 중지한 뒤 미확인 주문을 검토하세요.')
        try:
            return await broker.confirm_unsubmitted(
                payload.client_order_id, confirmed=payload.confirm_no_broker_order)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail='증권사 주문내역 조회 실패 · 기록을 보존했습니다.') from exc

    @app.get('/api/v1/live/positions')
    async def live_positions(request: Request) -> dict:
        return {'positions': await require_live_access(request).get_positions()}

    @app.get('/api/v1/live/account')
    async def live_account(request: Request) -> dict:
        broker = require_live_access(request)
        try:
            result = await broker.reconcile(recover_unfinished_orders=False)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'LIVE 계좌 동기화 실패: {type(exc).__name__}') from exc
        if not result.get('reconciled'):
            details = '; '.join(result.get('mismatches', []))
            raise HTTPException(status_code=409, detail='계좌 잔액 동기화 실패: ' + details)
        account = broker.account(get_engine(request).quotes)
        account['buying_power'] = dict(account['cash'])
        account['last_sync_at'] = broker.last_sync_at.isoformat() if broker.last_sync_at else None
        account['positions'] = await get_engine(request).with_stock_names(account['positions'])
        return account

    @app.get('/api/v1/live/orders')
    async def live_orders(request: Request, order_status: str = Query(default='OPEN')) -> dict:
        return {'orders': await require_live_access(request).list_orders(order_status)}

    @app.post('/api/v1/live/orders/{order_id}/cancel')
    async def cancel_live_order(order_id: str, request: Request) -> dict:
        broker = require_live_access(request)
        if get_engine(request).running:
            raise HTTPException(status_code=409, detail='Stop the trading engine before manually canceling an order.')
        try:
            order = await broker.cancel_and_resolve_order(order_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            'order': serialize(order),
            'cancel_confirmed': order.status in {OrderStatus.CANCELED, OrderStatus.PARTIALLY_FILLED},
            'fully_filled': order.status is OrderStatus.FILLED,
        }

    @app.post('/api/v1/live/test-limit-order/submit')
    async def submit_live_test_limit_order(payload: LiveLimitOrderTestInput, request: Request) -> dict:
        broker = require_live_access(request)
        engine = get_engine(request)
        if engine.running:
            raise HTTPException(status_code=409, detail='Stop the trading engine before a test order.')
        if engine.kill_switch:
            raise HTTPException(status_code=409, detail='Disable the kill switch before a test order.')
        if not broker.settings or not broker.settings.live_trading_enabled:
            raise HTTPException(status_code=409, detail='LIVE order submission is disabled.')
        if not broker.risk or not broker.risk.armed or not broker.reconciled:
            raise HTTPException(status_code=409, detail='Reconcile and arm LIVE before a test order.')
        try:
            quote = await engine.lookup_quote(payload.symbol)
            order = await broker.submit_limit_order(
                client_order_id='manual-limit-test-' + uuid4().hex,
                quote=quote,
                side=payload.side,
                quantity=Decimal(payload.quantity),
                limit_price=payload.limit_price,
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            'order': order,
            'accepted': True,
            'cancel_path': f"/api/v1/live/orders/{url_quote(str(order['orderId']), safe='')}/cancel",
        }

    @app.post('/api/v1/live/test-limit-order')
    async def live_test_limit_order(payload: LiveLimitOrderTestInput, request: Request) -> dict:
        broker = require_live_access(request)
        engine = get_engine(request)
        if engine.running:
            raise HTTPException(status_code=409, detail='Stop the trading engine before a test order.')
        if engine.kill_switch:
            raise HTTPException(status_code=409, detail='Disable the kill switch before a test order.')
        if not broker.settings or not broker.settings.live_trading_enabled:
            raise HTTPException(status_code=409, detail='LIVE order submission is disabled.')
        if not broker.risk or not broker.risk.armed or not broker.reconciled:
            raise HTTPException(status_code=409, detail='Reconcile and arm LIVE before a test order.')
        try:
            quote = await engine.lookup_quote(payload.symbol)
            order = await broker.place_limit_order(
                client_order_id='manual-limit-test-' + uuid4().hex,
                quote=quote,
                side=payload.side,
                quantity=Decimal(payload.quantity),
                limit_price=payload.limit_price,
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            'order': serialize(order),
            'cancel_confirmed': order.status.value in {'CANCELED', 'PARTIALLY_FILLED'},
            'fully_filled': order.status.value == 'FILLED',
        }

    @app.get("/api/v1/weekly-report")
    async def weekly_report(
        request: Request,
        period: str = Query(default="daily", pattern=r"^(daily|weekly)$"),
        refresh: bool = False,
    ) -> dict:
        return await request.app.state.weekly_report.report(period=period, force=refresh)

    @app.post("/api/v1/engine/start")
    async def start_engine(request: Request) -> dict:
        engine = get_engine(request)
        if request.app.state.settings.mode == 'live':
            require_live_access(request)
        try:
            await engine.start()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return engine.status()

    @app.post("/api/v1/engine/stop")
    async def stop_engine(request: Request) -> dict:
        engine = get_engine(request)
        if request.app.state.settings.mode == 'live':
            require_live_access(request)
        await engine.stop()
        broker = getattr(request.app.state, 'live_broker', None)
        if broker:
            broker.disarm()
        return engine.status()

    @app.post("/api/v1/engine/tick")
    async def tick_engine(request: Request) -> dict:
        engine = get_engine(request)
        if request.app.state.settings.mode == 'live':
            require_live_access(request)
        if engine.running:
            raise HTTPException(status_code=409, detail="실행 중인 엔진은 자동으로 tick합니다.")
        if engine.kill_switch:
            raise HTTPException(status_code=409, detail="킬 스위치가 활성화되어 있습니다.")
        await engine.tick()
        return engine.status()

    @app.post("/api/v1/risk/kill-switch")
    async def activate_kill_switch(request: Request) -> dict:
        engine = get_engine(request)
        if request.app.state.settings.mode == 'live':
            require_live_access(request)
        await engine.activate_kill_switch()
        broker = getattr(request.app.state, 'live_broker', None)
        if broker:
            broker.disarm()
        return engine.status()

    @app.post("/api/v1/risk/kill-switch/clear")
    async def clear_kill_switch(request: Request) -> dict:
        engine = get_engine(request)
        if request.app.state.settings.mode == 'live':
            require_live_access(request)
        try:
            await engine.clear_kill_switch()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return engine.status()

    @app.get("/api/v1/paper/account")
    async def paper_account(request: Request) -> dict:
        engine = get_engine(request)
        if request.app.state.settings.mode != 'paper':
            raise HTTPException(status_code=409, detail='PAPER 계좌 API는 PAPER 모드에서만 사용할 수 있습니다.')
        account = engine.broker.account(engine.quotes)
        account["positions"] = await engine.with_stock_names(account["positions"])
        return account

    @app.get("/api/v1/performance")
    async def performance(request: Request) -> dict:
        engine = get_engine(request)
        return engine.broker.performance(engine.quotes)

    @app.post("/api/v1/paper/positions/{symbol}/close")
    async def close_position(symbol: str, request: Request) -> dict:
        engine = get_engine(request)
        if engine.settings.mode != 'paper':
            raise HTTPException(status_code=409, detail='PAPER 주문 API는 PAPER 모드에서만 사용할 수 있습니다.')
        if not engine.automation:
            raise HTTPException(status_code=409, detail='LIVE 읽기 전용 모드에서는 주문할 수 없습니다.')
        try:
            orders = await engine.automation.manual_close(symbol.strip().upper())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logging.getLogger(__name__).warning("Manual close failed: %s", exc)
            raise HTTPException(status_code=502, detail="매도를 완료하지 못했습니다. 보유 수량과 시세 연결을 확인하세요.") from exc
        return {"orders": serialize(orders), "message": "수동 전량 매도 완료"}

    @app.get("/api/v1/risk/status")
    async def risk_status(request: Request) -> dict:
        engine = get_engine(request)
        settings = request.app.state.settings
        return {
            "kill_switch": engine.kill_switch,
            "max_order_amount": {
                "KRW": str(settings.live_max_order_amount_krw if settings.mode == 'live' else settings.max_order_amount_krw),
                "USD": str(settings.max_order_amount_usd),
            },
            "fee_rate": str(settings.fee_rate),
            "slippage_bps": str(settings.slippage_bps),
            "recommended_trade_ratio": str(settings.recommended_trade_ratio),
            "manual_trade_ratio": str(settings.manual_trade_ratio),
            "swing_exit_policy": {
                "stop_loss_enabled": settings.swing_stop_loss_enabled,
                "stop_loss_percent": str(settings.live_max_position_loss_percent if settings.mode == 'live' else Decimal(3)),
                "min_net_profit_percent": str(settings.swing_min_net_profit_percent),
                "estimated_fee_rate": str(settings.fee_rate),
                "estimated_sell_tax_rate": str(settings.live_sell_tax_rate if settings.mode == 'live' else Decimal(0)),
                "slippage_bps": str(settings.slippage_bps),
                "trailing_drawdown_percent": "2",
                "trailing_activation": "projected_net_profit_at_trigger",
                "protective_exit_below_minimum": True,
            },
            "swing_averaging_policy": {
                "enabled": settings.swing_averaging_enabled,
                "trigger_loss_percent": str(settings.swing_averaging_trigger_percent),
                "max_additions_per_position": 1,
                "amount_basis": "initial_purchase_cost_with_estimated_fee",
                "requires_fresh_entry_signal": False,
                "resets_trailing_after_fill": True,
                "live_order_type": "LIMIT",
            },
            "live_limits": {
                "allowed_symbols": (sorted(getattr(getattr(engine.broker, 'risk', None), 'recommended_symbols', ()))
                                    if settings.live_symbol_policy == 'recommended' else list(settings.live_allowed_symbols)),
                "symbol_policy": settings.live_symbol_policy,
                "max_order_amount_krw": str(settings.live_max_order_amount_krw),
                "max_total_exposure_krw": str(settings.live_max_total_exposure_krw),
                "max_total_exposure_ratio": str(settings.live_max_total_exposure_ratio),
                "auto_max_total_exposure_ratio": str(settings.live_auto_max_total_exposure_ratio),
                "allocation_mode": settings.live_auto_allocation_mode,
                "budget_split": settings.live_auto_budget_split,
                "max_buy_ratio": str(settings.live_max_buy_ratio),
                "min_cash_ratio": str(settings.live_min_cash_ratio),
                "effective_total_exposure_ratio": str(settings.live_exposure_ratio_limit),
                "effective_auto_exposure_ratio": str(settings.live_auto_exposure_ratio_limit),
                "daily_loss_limit_enabled": settings.live_daily_loss_limit_enabled,
                "max_daily_loss_krw": str(settings.live_max_daily_loss_krw),
                "daily_loss_krw": str(engine.broker.risk.daily_loss),
                "daily_loss_limit_reached": engine.broker.risk.daily_loss_limit_reached,
                "daily_equity_date": engine.broker.daily_equity_date,
            } if settings.mode == 'live' else None,
        }

    @app.put("/api/v1/settings/investment-ratio")
    async def update_investment_ratio(payload: InvestmentRatioInput, request: Request) -> dict:
        ratio = payload.ratio_percent / Decimal("100")
        if request.app.state.settings.mode == 'live':
            require_live_access(request)
            maximum = request.app.state.settings.live_auto_ratio_limit * 100
            if payload.ratio_percent > maximum:
                raise HTTPException(status_code=422, detail=f'LIVE 자동매매 투자비율은 {maximum:g}% 이하로 설정하세요.')
        elif payload.ratio_percent > 50:
            raise HTTPException(status_code=422, detail='PAPER 투자비율은 50% 이하로 설정하세요.')
        await save_trade_ratio(request, 'recommended_trade_ratio', ratio)
        return {"ratio_percent": str(payload.ratio_percent), "recommended_trade_ratio": str(ratio)}

    @app.put('/api/v1/settings/manual-investment-ratio')
    async def update_manual_investment_ratio(payload: InvestmentRatioInput, request: Request) -> dict:
        settings = request.app.state.settings
        if settings.mode == 'live':
            require_live_access(request)
            if payload.ratio_percent > settings.live_manual_ratio_limit * 100:
                raise HTTPException(status_code=422, detail=f'직접 매수 비율은 {settings.live_manual_ratio_limit * 100:g}% 이하여야 합니다.')
        ratio = payload.ratio_percent / Decimal(100)
        await save_trade_ratio(request, 'manual_trade_ratio', ratio)
        return {'ratio_percent': str(payload.ratio_percent), 'manual_trade_ratio': str(ratio)}

    async def save_trade_ratio(request: Request, key: str, ratio: Decimal):
        async with trade_ratio_lock:
            settings = replace(request.app.state.settings, **{key: ratio})
            preferences = await request.app.state.repository.load('trading_preferences') or {}
            preferences[key] = str(ratio)
            await request.app.state.repository.save('trading_preferences', preferences)
            request.app.state.settings = settings
            engine = get_engine(request)
            engine.settings = settings
            if hasattr(engine.broker, "settings"):
                engine.broker.settings = settings
            if getattr(engine.broker, 'risk', None):
                engine.broker.risk.settings = settings

    @app.post("/api/v1/paper/reset")
    async def reset_paper_account(payload: PaperResetInput, request: Request) -> dict:
        engine = get_engine(request)
        if request.app.state.settings.mode != 'paper':
            raise HTTPException(status_code=409, detail='LIVE 모드에서는 PAPER 계좌를 초기화할 수 없습니다.')
        if engine.running:
            raise HTTPException(status_code=409, detail="엔진을 중지한 뒤 초기화하세요.")
        await engine.broker.reset(payload.cash_krw, payload.cash_usd)
        await engine.automation.reset()
        return engine.broker.account(engine.quotes)

    @app.get("/api/v1/orders")
    async def list_orders(request: Request, limit: int = 100) -> dict:
        if not 1 <= limit <= 1000:
            raise HTTPException(status_code=400, detail="limit은 1~1000이어야 합니다.")
        engine = get_engine(request)
        orders = engine.broker.orders[-limit:]
        return {"orders": await engine.with_stock_names(serialize(list(reversed(orders))))}

    @app.get("/api/v1/market/quotes")
    async def list_quotes(request: Request) -> dict:
        engine = get_engine(request)
        return {"quotes": await engine.with_stock_names(serialize(list(engine.quotes.values())))}

    @app.get("/api/v1/market/lookup")
    async def lookup_market(
        request: Request,
        symbol: str = Query(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$"),
    ) -> dict:
        engine = get_engine(request)
        try:
            quote = await engine.lookup_quote(symbol)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="조회된 시세가 없습니다.") from exc
        except Exception as exc:
            engine.last_error = f"market-lookup: {type(exc).__name__}: {exc}"
            raise HTTPException(
                status_code=502, detail="토스 시세 조회에 실패했습니다. 종목과 API 설정을 확인하세요."
            ) from exc
        stock = None
        day_open = None
        previous_close = None
        if engine.toss_client:
            try:
                stock = await engine.toss_client.stock_info(quote.symbol)
            except Exception as exc:
                logging.getLogger(__name__).warning("Stock info lookup failed: %s", exc)
            try:
                daily_candles = await engine.candles(quote.symbol, "1d", 2)
                if daily_candles:
                    day_open = daily_candles[-1].open_price
                if len(daily_candles) >= 2:
                    previous_close = daily_candles[-2].close_price
            except Exception as exc:
                logging.getLogger(__name__).warning("Daily open lookup failed: %s", exc)
        return {
            "quote": serialize(quote),
            "day_open": str(day_open) if day_open is not None else None,
            "previous_close": str(previous_close) if previous_close is not None else None,
            "stock": {
                "symbol": stock.get("symbol"),
                "name": stock.get("name"),
                "market": stock.get("market"),
            } if stock else None,
        }

    @app.get("/api/v1/market/search")
    async def search_market(
        request: Request,
        q: str = Query(min_length=1, max_length=50),
        limit: int = Query(default=10, ge=1, le=20),
    ) -> dict:
        engine = get_engine(request)
        if not engine.toss_client:
            raise HTTPException(status_code=409, detail="한글 종목 검색은 토스 API 연결이 필요합니다.")
        try:
            stocks = await engine.toss_client.search_stocks(q, limit)
        except Exception as exc:
            engine.last_error = f"stock-search: {type(exc).__name__}: {exc}"
            raise HTTPException(status_code=502, detail="종목명 검색에 실패했습니다.") from exc
        return {
            "query": q,
            "results": [
                {
                    "symbol": stock.get("symbol"),
                    "name": stock.get("name"),
                    "market": stock.get("market"),
                    "security_type": stock.get("securityType"),
                }
                for stock in stocks
            ],
        }

    @app.get("/api/v1/recommendations")
    async def recommendations(request: Request) -> dict:
        engine = get_engine(request)
        if not engine.toss_client:
            raise HTTPException(status_code=409, detail="추천 후보 탐색은 토스 API 연결이 필요합니다.")
        result = await build_recommendations(engine, request.app.state.weekly_report.cached_direction())
        if str(getattr(engine.automation, 'strategy', '')).startswith('swing-v'):
            await engine.automation.store_observations(result, 'manual')
        return result

    @app.get("/api/v1/swing/signal-history")
    async def signal_history(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> dict:
        engine = get_engine(request)
        rows = await engine.repository.recent_signal_observations(limit)
        return {"count": await engine.repository.signal_observation_count(), "observations": rows}

    @app.post("/api/v1/paper/test-buy/{symbol}")
    async def test_buy(symbol: str, request: Request) -> dict:
        engine = get_engine(request)
        try:
            order = await engine.automation.test_buy(symbol.strip().upper())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logging.getLogger(__name__).warning("PAPER test buy failed: %s", exc)
            raise HTTPException(status_code=502, detail="PAPER 테스트 매수를 완료하지 못했습니다.") from exc
        return {"order": serialize(order), "message": "PAPER 테스트 1주 매수 완료"}

    @app.post("/api/v1/paper/qualified-buy/{symbol}")
    async def qualified_buy(symbol: str, request: Request, payload: ManualBuyInput | None = None) -> dict:
        engine = get_engine(request)
        if engine.settings.mode != 'paper':
            raise HTTPException(status_code=409, detail='PAPER 주문 API는 PAPER 모드에서만 사용할 수 있습니다.')
        try:
            kwargs = {'ratio': payload.ratio_percent / 100} if payload and payload.ratio_percent is not None else {}
            order = await engine.automation.buy_qualified(symbol.strip().upper(), **kwargs)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logging.getLogger(__name__).warning("PAPER qualified buy failed: %s", exc)
            raise HTTPException(status_code=502, detail="조건 통과 종목 매수를 완료하지 못했습니다.") from exc
        return {"order": serialize(order), "message": "자산 비율 PAPER 매수 완료"}

    @app.post('/api/v1/live/qualified-buy/{symbol}')
    async def live_qualified_buy(symbol: str, payload: LiveBuyInput, request: Request) -> dict:
        require_live_access(request)
        engine = get_engine(request)
        if not engine.automation:
            raise HTTPException(status_code=409, detail='LIVE 자동매매 주문이 활성화되지 않았습니다.')
        ratio_percent = getattr(payload, 'ratio_percent', None)
        if ratio_percent is not None and ratio_percent > engine.settings.live_max_total_exposure_ratio * 100:
            raise HTTPException(status_code=422, detail='직접 매수 비율은 LIVE 총자산 한도 이하여야 합니다.')
        try:
            kwargs = {'ratio': ratio_percent / 100} if ratio_percent is not None else {}
            order = await engine.automation.buy_qualified(symbol.strip().upper(), **kwargs)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail='LIVE 매수 결과를 확인하지 못했습니다. 주문 내역과 계좌 동기화 상태를 확인하세요.') from exc
        return {'order': serialize(order), 'message': f'LIVE 매수 결과: {order.status.value} · 체결 {order.quantity}주'}

    @app.post('/api/v1/live/positions/{symbol}/close')
    async def live_close_position(symbol: str, payload: LiveTradeInput, request: Request) -> dict:
        require_live_access(request)
        engine = get_engine(request)
        if not engine.automation:
            raise HTTPException(status_code=409, detail='LIVE 자동매매 주문이 활성화되지 않았습니다.')
        try:
            orders = await engine.automation.manual_close(symbol.strip().upper())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail='LIVE 매도 결과를 확인하지 못했습니다. 주문 내역과 보유 수량을 확인하세요.') from exc
        completed = all(o.status is OrderStatus.FILLED for o in orders)
        return {'orders': serialize(orders), 'message': 'LIVE 전량 매도 완료' if completed else 'LIVE 매도 일부 체결 또는 미체결 · 주문 내역 확인'}

    @app.get("/api/v1/market/candles")
    async def market_candles(
        request: Request,
        symbol: str = Query(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$"),
        interval: str = Query(
            default="1d", pattern=r"^(15m|30m|60m|4h|1d|1w|1M)$"
        ),
        count: int = Query(default=250, ge=1, le=300),
    ) -> dict:
        engine = get_engine(request)
        try:
            candles = await engine.candles(symbol, interval, count)
        except TossCandleRateLimitError as exc:
            engine.last_error = f"market-candles: {exc}"
            raise HTTPException(
                status_code=429,
                detail=f'토스 캔들 조회 요청 한도를 초과했습니다. {exc.retry_after}초 후 다시 조회해 주세요.',
                headers={'Retry-After': str(exc.retry_after)},
            ) from exc
        except Exception as exc:
            engine.last_error = f"market-candles: {type(exc).__name__}: {exc}"
            raise HTTPException(
                status_code=502, detail="캔들 조회에 실패했습니다. 종목과 API 설정을 확인하세요."
            ) from exc
        return {"symbol": symbol.upper(), "interval": interval, "candles": serialize(candles)}

    @app.post("/api/v1/market/refresh")
    async def refresh_market(request: Request) -> dict:
        engine = get_engine(request)
        try:
            quotes = await engine.refresh_market_data()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            engine.last_error = f"market-data: {type(exc).__name__}: {exc}"
            raise HTTPException(
                status_code=502, detail="토스 시세 조회에 실패했습니다. 설정과 허용 IP를 확인하세요."
            ) from exc
        return {"quotes": serialize(quotes)}

    @app.post("/api/v1/market/quotes", status_code=status.HTTP_201_CREATED)
    async def update_quote(payload: QuoteInput, request: Request) -> dict:
        engine = get_engine(request)
        quote = Quote(
            symbol=payload.symbol.upper(),
            price=payload.price,
            currency=payload.currency,
            timestamp=utc_now(),
            source="manual",
        )
        engine.set_quote(quote)
        return serialize(quote)

    @app.websocket("/ws/market/trades")
    async def realtime_trades(websocket: WebSocket, symbol: str) -> None:
        await websocket.accept()
        engine: TradingEngine = websocket.app.state.engine
        normalized = symbol.strip().upper()
        if not normalized or len(normalized) > 20 or not all(
            char.isalnum() or char in ".-" for char in normalized
        ):
            await websocket.send_json({"type": "error", "message": "잘못된 종목 코드입니다."})
            await websocket.close(code=1008)
            return
        if not engine.toss_client:
            await websocket.send_json({"type": "error", "message": "토스 시세 API가 비활성화되어 있습니다."})
            await websocket.close(code=1008)
            return
        try:
            initial_quote = await engine.lookup_quote(normalized)
            async for trade in engine.toss_client.trade_stream(normalized, initial_quote.currency):
                if trade.get("_type") == "connected":
                    await websocket.send_json({"type": "connected", "symbol": normalized})
                    continue
                quote = Quote(
                    symbol=normalized,
                    price=Decimal(trade["price"]),
                    currency=Currency(trade["currency"]),
                    timestamp=datetime.fromisoformat(trade["timestamp"]),
                    source="toss",
                )
                engine.set_quote(quote)
                await websocket.send_json(
                    {
                        "type": "trade",
                        "symbol": normalized,
                        "price": str(quote.price),
                        "volume": str(trade["volume"]),
                        "timestamp": quote.timestamp.isoformat(),
                        "currency": quote.currency.value,
                    }
                )
        except WebSocketDisconnect:
            return
        except Exception as exc:
            logging.getLogger(__name__).warning("Realtime trade stream failed: %s", exc)
            try:
                await websocket.send_json({"type": "error", "message": "실시간 시세 연결이 끊어졌습니다."})
                await websocket.close(code=1011)
            except Exception:
                pass

    @app.get("/api/v1/strategies")
    async def list_strategies(request: Request) -> dict:
        return {"strategies": serialize(list(get_engine(request).strategies.values()))}

    @app.post("/api/v1/strategies", status_code=status.HTTP_201_CREATED)
    async def upsert_strategy(payload: StrategyInput, request: Request) -> dict:
        engine = get_engine(request)
        strategy = ThresholdStrategy(
            strategy_id=payload.strategy_id or f"threshold-{uuid4().hex[:10]}",
            symbol=payload.symbol.upper(),
            currency=payload.currency,
            quantity=payload.quantity,
            buy_below=payload.buy_below,
            sell_above=payload.sell_above,
            enabled=payload.enabled,
        )
        await engine.upsert_strategy(strategy)
        return serialize(strategy)

    @app.post("/api/v1/strategies/{strategy_id}/enable")
    async def enable_strategy(strategy_id: str, request: Request) -> dict:
        engine = get_engine(request)
        try:
            await engine.set_strategy_enabled(strategy_id, True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="전략을 찾을 수 없습니다.") from exc
        return serialize(engine.strategies[strategy_id])

    @app.post("/api/v1/strategies/{strategy_id}/disable")
    async def disable_strategy(strategy_id: str, request: Request) -> dict:
        engine = get_engine(request)
        try:
            await engine.set_strategy_enabled(strategy_id, False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="전략을 찾을 수 없습니다.") from exc
        return serialize(engine.strategies[strategy_id])

    return app


app = create_app()
