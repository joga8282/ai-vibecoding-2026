from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from app.config import Settings
from app.engine import TradingEngine
from app.models import Currency, Quote, ThresholdStrategy, serialize, utc_now
from app.paper import PaperBroker
from app.repository import SnapshotRepository
from app.toss import TossMarketClient

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


def get_engine(request: Request) -> TradingEngine:
    return request.app.state.engine


def create_app(settings: Settings | None = None) -> FastAPI:
    selected_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository = SnapshotRepository(selected_settings.database_path)
        await repository.initialize()
        broker = PaperBroker(selected_settings, repository)
        await broker.restore()
        toss_client = None
        if selected_settings.toss_client_id and selected_settings.toss_client_secret:
            toss_client = TossMarketClient(
                selected_settings.toss_client_id, selected_settings.toss_client_secret
            )
        engine = TradingEngine(selected_settings, broker, repository, toss_client)
        await engine.restore()
        app.state.settings = selected_settings
        app.state.repository = repository
        app.state.broker = broker
        app.state.engine = engine
        if selected_settings.auto_start:
            await engine.start()
        yield
        await engine.stop()

    app = FastAPI(
        title=selected_settings.app_name,
        version=selected_settings.version,
        description=(
            "토스증권 시세를 선택적으로 사용하는 paper-only 자동매매 MVP. "
            "v0.1에는 실제 주문 기능이 없습니다."
        ),
        lifespan=lifespan,
    )

    @app.get("/health/live")
    async def health_live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request) -> dict:
        engine = get_engine(request)
        return {
            "status": "ready",
            "mode": "paper",
            "market_data": "toss" if engine.toss_client else "manual",
        }

    @app.get("/api/v1/system/status")
    async def system_status(request: Request) -> dict:
        return get_engine(request).status()

    @app.post("/api/v1/engine/start")
    async def start_engine(request: Request) -> dict:
        engine = get_engine(request)
        try:
            await engine.start()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return engine.status()

    @app.post("/api/v1/engine/stop")
    async def stop_engine(request: Request) -> dict:
        engine = get_engine(request)
        await engine.stop()
        return engine.status()

    @app.post("/api/v1/engine/tick")
    async def tick_engine(request: Request) -> dict:
        engine = get_engine(request)
        if engine.running:
            raise HTTPException(status_code=409, detail="실행 중인 엔진은 자동으로 tick합니다.")
        if engine.kill_switch:
            raise HTTPException(status_code=409, detail="킬 스위치가 활성화되어 있습니다.")
        await engine.tick()
        return engine.status()

    @app.post("/api/v1/risk/kill-switch")
    async def activate_kill_switch(request: Request) -> dict:
        engine = get_engine(request)
        await engine.activate_kill_switch()
        return engine.status()

    @app.post("/api/v1/risk/kill-switch/clear")
    async def clear_kill_switch(request: Request) -> dict:
        engine = get_engine(request)
        try:
            await engine.clear_kill_switch()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return engine.status()

    @app.get("/api/v1/paper/account")
    async def paper_account(request: Request) -> dict:
        engine = get_engine(request)
        return engine.broker.account(engine.quotes)

    @app.post("/api/v1/paper/reset")
    async def reset_paper_account(payload: PaperResetInput, request: Request) -> dict:
        engine = get_engine(request)
        if engine.running:
            raise HTTPException(status_code=409, detail="엔진을 중지한 뒤 초기화하세요.")
        await engine.broker.reset(payload.cash_krw, payload.cash_usd)
        return engine.broker.account(engine.quotes)

    @app.get("/api/v1/orders")
    async def list_orders(request: Request, limit: int = 100) -> dict:
        if not 1 <= limit <= 1000:
            raise HTTPException(status_code=400, detail="limit은 1~1000이어야 합니다.")
        orders = get_engine(request).broker.orders[-limit:]
        return {"orders": serialize(list(reversed(orders)))}

    @app.get("/api/v1/market/quotes")
    async def list_quotes(request: Request) -> dict:
        return {"quotes": serialize(list(get_engine(request).quotes.values()))}

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
