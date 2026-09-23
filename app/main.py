from __future__ import annotations

import logging
import asyncio
import random
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app.config import Settings
from app.engine import TradingEngine
from app.models import Currency, Quote, ThresholdStrategy, serialize, utc_now
from app.paper import PaperBroker
from app.repository import SnapshotRepository
from app.recommendations import analyze_candidate
from app.toss import TossMarketClient
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
        if (
            selected_settings.toss_market_data_enabled
            and selected_settings.toss_client_id
            and selected_settings.toss_client_secret
        ):
            toss_client = TossMarketClient(
                selected_settings.toss_client_id, selected_settings.toss_client_secret
            )
        engine = TradingEngine(selected_settings, broker, repository, toss_client)
        await engine.restore()
        app.state.settings = selected_settings
        app.state.repository = repository
        app.state.broker = broker
        app.state.engine = engine
        app.state.weekly_report = WeeklyReportService()
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
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(static_dir / "index.html")

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

    @app.get("/api/v1/performance")
    async def performance(request: Request) -> dict:
        engine = get_engine(request)
        return engine.broker.performance(engine.quotes)

    @app.get("/api/v1/risk/status")
    async def risk_status(request: Request) -> dict:
        engine = get_engine(request)
        settings = request.app.state.settings
        return {
            "kill_switch": engine.kill_switch,
            "max_order_amount": {
                "KRW": str(settings.max_order_amount_krw),
                "USD": str(settings.max_order_amount_usd),
            },
            "fee_rate": str(settings.fee_rate),
            "slippage_bps": str(settings.slippage_bps),
            "recommended_trade_ratio": str(settings.recommended_trade_ratio),
        }

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
        account = engine.broker.account(engine.quotes)
        equity = Decimal(account["total_equity"]["KRW"])
        cash = Decimal(account["cash"]["KRW"])
        budget = min(cash, equity * engine.settings.recommended_trade_ratio)
        try:
            ranking_result = await engine.toss_client.rankings(100)
            ranking_items = [
                item for item in ranking_result.get("rankings", [])
                if item.get("currency") == "KRW"
                and Decimal(item.get("price", {}).get("lastPrice", "0")) > 0
                and Decimal(item.get("price", {}).get("lastPrice", "0")) <= budget
            ][:30]
            symbols = [item["symbol"] for item in ranking_items]
            stock_items = await engine.toss_client.stocks_info(symbols)
            stocks_by_symbol = {item["symbol"]: item for item in stock_items}
            risk_filtered: list[tuple[dict, dict, Decimal, Decimal]] = []
            for ranking in ranking_items:
                stock = stocks_by_symbol.get(ranking["symbol"], {})
                if stock.get("securityType") != "STOCK" or stock.get("isCommonShare") is not True:
                    continue
                price = Decimal(ranking["price"]["lastPrice"])
                market_cap = price * Decimal(stock.get("sharesOutstanding") or "0")
                if market_cap >= Decimal("1000000000000"):
                    risk_filtered.append((ranking, stock, price, market_cap))
            # Analyze a different pre-screened subset on every request.
            eligible = random.sample(risk_filtered, min(12, len(risk_filtered)))
            candle_results = await asyncio.wait_for(
                asyncio.gather(
                    *(engine.toss_client.candles(item[0]["symbol"], "1d", 120) for item in eligible),
                    return_exceptions=True,
                ),
                timeout=20,
            )
        except Exception as exc:
            engine.last_error = f"recommendations: {type(exc).__name__}: {exc}"
            raise HTTPException(status_code=502, detail="추천 후보를 분석하지 못했습니다.") from exc

        candidates: list[dict] = []
        report_direction = request.app.state.weekly_report.cached_direction()
        macro_adjustment = 3 if report_direction == "risk_on" else -7 if report_direction == "defensive" else 0
        for (ranking, stock, price, market_cap), candles in zip(eligible, candle_results):
            if isinstance(candles, Exception):
                continue
            change_rate = Decimal(ranking["price"].get("changeRate", "0"))
            analysis = analyze_candidate(candles, price, change_rate)
            if not analysis or not analysis["support_touched"]:
                continue
            analysis["score"] = max(0, min(100, analysis["score"] + macro_adjustment))
            base_quantity = int(budget // price)
            suggested_quantity = base_quantity // 2 if report_direction == "defensive" else base_quantity
            candidates.append(
                {
                    "symbol": ranking["symbol"],
                    "name": stock.get("name") or ranking["symbol"],
                    "price": str(price),
                    "market_cap": str(market_cap.quantize(Decimal("1"))),
                    "currency": "KRW",
                    "quantity": suggested_quantity,
                    "change_rate": str(change_rate),
                    "weekly_direction": report_direction,
                    "macro_adjustment": macro_adjustment,
                    **analysis,
                }
            )
        random.shuffle(candidates)
        selected_candidates = candidates[:5]
        return {
            "budget": str(budget.quantize(Decimal("1"))),
            "ratio": str(engine.settings.recommended_trade_ratio),
            "ranked_at": ranking_result.get("rankedAt"),
            "weekly_direction": report_direction,
            "funnel": {
                "universe": int(ranking_result.get("totalCount") or 2601),
                "budget_liquidity": len(ranking_items),
                "risk_filtered": len(eligible),
                "analyzed": len(candle_results),
                "qualified": len(candidates),
            },
            "candidates": selected_candidates,
            "disclaimer": "일봉·주봉 지지 확인과 저항 여력을 통과한 PAPER 후보이며 투자 권유가 아닙니다.",
        }

    @app.get("/api/v1/market/candles")
    async def market_candles(
        request: Request,
        symbol: str = Query(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$"),
        interval: str = Query(
            default="1d", pattern=r"^(1d|1w|1M)$"
        ),
        count: int = Query(default=250, ge=1, le=300),
    ) -> dict:
        engine = get_engine(request)
        try:
            candles = await engine.candles(symbol, interval, count)
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
