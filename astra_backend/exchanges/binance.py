"""Binance USDⓈ-M adapter backed exclusively by Binance's official SDK.

The adapter deliberately does not know how Binance REST requests are signed or
transported.  ``binance-sdk-derivatives-trading-usds-futures`` owns that
boundary, including authentication, retries, timeout handling and response
models.  This module only translates the SDK models into Astra's exchange
adapter contract.
"""
from __future__ import annotations

import re
import time
import uuid
import warnings
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from .base import (
    BaseExchangeAdapter,
    ExchangeCapabilities,
    ExchangeCapabilityError,
    InstrumentSpec,
)
from .binance_orders import build_order_params, validate_entry_limit_notional


def interpret_dual_side_position(payload: Any) -> str:
    """Translate Binance's ``dualSidePosition`` response to Astra's vocabulary."""
    if not isinstance(payload, dict):
        return "unknown"
    value = payload.get("dualSidePosition")
    if isinstance(value, bool):
        return "long_short" if value else "net"
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return "long_short" if value.strip().lower() == "true" else "net"
    return "unknown"


INTERVAL_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "1h",
    "2H": "2h",
    "4H": "4h",
    "6H": "6h",
    "12H": "12h",
    "1D": "1d",
    "1W": "1w",
}


class BinanceAPIError(RuntimeError):
    """Binance SDK/API failure normalized for Astra callers."""

    def __init__(self, code: Any, message: str, status: int = 0):
        super().__init__(f"Binance [{code}]: {message or 'request failed'}")
        self.code = code
        self.message = message
        self.status = status


class BinanceAdapter(BaseExchangeAdapter):
    """USDⓈ-M adapter using the official Binance connector package."""

    base_url = "https://fapi.binance.com"
    live_url = "https://fapi.binance.com"
    test_url = "https://demo-fapi.binance.com"

    capabilities = ExchangeCapabilities(
        venue="binance",
        display_name="Binance 币安 USDT-M 合约",
        symbol_template="{base}USDT",
        quantity_unit="base_asset",
        signed_size=False,
        supports_attached_tp_sl=False,
        trigger_price_default="explicit_only",
        max_candle_limit=1500,
        bar_case="lower",
        has_top_trader_ratio=True,
        has_taker_ratio=True,
        supports_account=True,
        supports_orders=True,
        adapter_execution_flag="ASTRA_BINANCE_EXECUTION",
        mainland_ip_restricted=True,
        rate_limit_note="请求频率、重试、签名与错误处理由 Binance 官方 SDK 统一管理",
        order_id_type="int64_precision_risk",
        native_amend=False,
        decimal_amount=False,
        position_modes=("net", "long_short"),
        entry_ready_position_modes=("net",),
        conditional_family="algo_service",
        protection_semantics=(
            "独立 algo 资源族（STOP/TP/TRAILING 等 CONDITIONAL）：普通 openOrders "
            "不代表保护单全集；closePosition=true 仅指定条件市价单"
        ),
    )

    _SERVER_TIME_CACHE: Dict[str, tuple] = {}
    SERVER_TIME_TTL_S = 300.0
    SERVER_TIME_WARN_MS = 3000.0

    def __init__(self, session=None, environment: Optional[str] = None) -> None:
        # ``session`` is accepted for BaseExchangeAdapter compatibility.  The
        # official SDK owns its requests.Session and does not accept an external
        # transport session.
        super().__init__(session=None, environment=environment)
        self._sdk_client = None
        self._sdk_identity = None

    # ------------------------------------------------------------------
    # Official SDK boundary
    # ------------------------------------------------------------------
    @staticmethod
    def _plain(value: Any) -> Any:
        """Convert SDK response models to the adapter's dict/list contract."""
        if isinstance(value, dict):
            return {key: BinanceAdapter._plain(item) for key, item in value.items()}
        if isinstance(value, list):
            return [BinanceAdapter._plain(item) for item in value]
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return BinanceAdapter._plain(to_dict())
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return BinanceAdapter._plain(model_dump(by_alias=True))
        return value

    @staticmethod
    def _sdk_error(exc: Exception) -> BinanceAPIError:
        message = getattr(exc, "error_message", None) or str(exc)
        code = getattr(exc, "code", None)
        if code is None:
            match = re.search(r'"?code"?\s*[:=]\s*(-?\d+)', message)
            code = int(match.group(1)) if match else "network"
        status = getattr(exc, "status_code", 0) or 0
        return BinanceAPIError(code, message, status=status)

    def _keys(self) -> tuple[str, str]:
        from .registry import venue_credentials

        key, secret = venue_credentials("binance", self.environment)
        if not key or not secret:
            raise ExchangeCapabilityError(
                f"Binance ({self.environment}档) 凭证未配置——请在后台「多交易所凭证」录入 API Key/Secret"
            )
        return key, secret

    def _client(self):
        """Build/cache an SDK client for the resolved environment and credentials."""
        from binance_common.configuration import ConfigurationRestAPI
        from binance_sdk_derivatives_trading_usds_futures.derivatives_trading_usds_futures import (
            DerivativesTradingUsdsFutures,
        )
        from .registry import venue_credentials

        key, secret = venue_credentials("binance", self.environment)
        identity = (key, secret, self.base_url)
        cached_client = getattr(self, "_sdk_client", None)
        cached_identity = getattr(self, "_sdk_identity", None)
        if cached_client is None or cached_identity != identity:
            config = ConfigurationRestAPI(
                api_key=key or None,
                api_secret=secret or None,
                base_path=self.base_url,
                timeout=4000,
                retries=3,
                backoff=250,
            )
            self._sdk_client = DerivativesTradingUsdsFutures(config_rest_api=config)
            self._sdk_identity = identity
        return self._sdk_client

    def _sdk_call(self, method: str, *, private: bool = False, **kwargs: Any) -> Any:
        """Call one generated SDK method; no adapter code constructs HTTP."""
        if private:
            self._keys()
        try:
            response = getattr(self._client().rest_api, method)(**kwargs)
            return self._plain(response.data())
        except BinanceAPIError:
            raise
        except Exception as exc:
            raise self._sdk_error(exc) from exc

    def _public_call(self, method: str, **kwargs: Any) -> Any:
        try:
            return self._sdk_call(method, **kwargs)
        except Exception:
            return None

    def _private_call(self, method: str, **kwargs: Any) -> Any:
        return self._sdk_call(method, private=True, **kwargs)

    @staticmethod
    def _record(data: Any) -> Optional[Dict[str, Any]]:
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _interval(self, bar: str) -> str:
        return INTERVAL_MAP.get(str(bar or "15m").strip(), str(bar or "15m").strip().lower())

    # ------------------------------------------------------------------
    # Public market data
    # ------------------------------------------------------------------
    def fetch_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        inst = self.native_symbol(symbol)
        stats = self._record(self._public_call("ticker24hr_price_change_statistics", symbol=inst))
        if not stats or not stats.get("lastPrice"):
            return None

        bid = ask = None
        book = self._record(self._public_call("symbol_order_book_ticker", symbol=inst))
        if book and book.get("bidPrice"):
            bid = self._number(book.get("bidPrice"))
            ask = self._number(book.get("askPrice"))
        else:
            depth = self._public_call("order_book", symbol=inst, limit=5)
            if isinstance(depth, dict) and depth.get("bids") and depth.get("asks"):
                bid = self._number(depth["bids"][0][0])
                ask = self._number(depth["asks"][0][0])

        return {
            "venue": "binance",
            "inst_id": inst,
            "last": self._number(stats.get("lastPrice")),
            "bid": bid,
            "ask": ask,
            "open_24h": self._number(stats.get("openPrice")) or None,
            "high_24h": self._number(stats.get("highPrice")) or None,
            "low_24h": self._number(stats.get("lowPrice")) or None,
            "chg_24h_pct": self._number(stats.get("priceChangePercent")),
            "vol_24h_base": self._number(stats.get("volume")),
            "quote_vol_24h": self._number(stats.get("quoteVolume")),
            "ts_ms": int(stats.get("closeTime") or time.time() * 1000),
        }

    def fetch_candles(self, symbol: str, bar: str = "15m",
                      limit: int = 100) -> Optional[List[List[Any]]]:
        data = self._public_call(
            "kline_candlestick_data",
            symbol=self.native_symbol(symbol),
            interval=self._sdk_interval(self._interval(bar)),
            limit=min(int(limit), self.capabilities.max_candle_limit),
        )
        if not isinstance(data, list) or not data:
            return None
        out = []
        for row in data:
            try:
                out.append([int(row[0]), str(row[1]), str(row[2]), str(row[3]),
                            str(row[4]), str(row[5])])
            except (IndexError, TypeError, ValueError):
                continue
        return out or None

    @staticmethod
    def _sdk_interval(interval: str):
        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
            KlineCandlestickDataIntervalEnum,
        )

        for item in KlineCandlestickDataIntervalEnum:
            if item.value == interval:
                return item
        raise ValueError(f"Binance SDK 不支持 K 线周期: {interval}")

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        data = self._record(self._public_call("mark_price", symbol=self.native_symbol(symbol)))
        if data and data.get("lastFundingRate") is not None:
            try:
                return float(data["lastFundingRate"])
            except (TypeError, ValueError):
                pass
        return None

    def fetch_orderbook(self, symbol: str, depth: int = 20) -> Optional[Dict[str, Any]]:
        data = self._public_call(
            "order_book", symbol=self.native_symbol(symbol), limit=int(depth)
        )
        if isinstance(data, dict) and data.get("bids"):
            return {"venue": "binance", "bids": data["bids"], "asks": data.get("asks", [])}
        return None

    def fetch_open_interest(self, symbol: str) -> Optional[float]:
        data = self._record(self._public_call("open_interest", symbol=self.native_symbol(symbol)))
        if data and data.get("openInterest") is not None:
            try:
                return float(data["openInterest"])
            except (TypeError, ValueError):
                pass
        return None

    @staticmethod
    def _market_period(enum_type, period: str):
        value = str(period or "5m").strip().lower()
        for item in enum_type:
            if item.value == value:
                return item
        raise ValueError(f"Binance SDK 不支持的统计周期: {period}")

    def fetch_taker_volume(self, symbol: str, *, period: str = "5m",
                           limit: int = 1) -> List[Dict[str, Any]]:
        """Return Binance taker buy/sell volume rows through the SDK."""
        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
            TakerBuySellVolumePeriodEnum,
        )

        data = self._public_call(
            "taker_buy_sell_volume",
            symbol=self.native_symbol(symbol),
            period=self._market_period(TakerBuySellVolumePeriodEnum, period),
            limit=int(limit),
        )
        return data if isinstance(data, list) else []

    def fetch_taker_ratio(self, symbol: str) -> Optional[float]:
        data = self.fetch_taker_volume(symbol, period="1h", limit=1)
        row = data[-1] if data else None
        if isinstance(row, dict):
            value = self._number(row.get("buySellRatio"), -1.0)
            return value if value > 0 else None
        return None

    def fetch_top_trader_position_ratio(
            self, symbol: str, *, period: str = "5m", limit: int = 1) -> List[Dict[str, Any]]:
        """Return top-trader position-ratio rows through the SDK."""
        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
            TopTraderLongShortRatioPositionsPeriodEnum,
        )

        data = self._public_call(
            "top_trader_long_short_ratio_positions",
            symbol=self.native_symbol(symbol),
            period=self._market_period(TopTraderLongShortRatioPositionsPeriodEnum, period),
            limit=int(limit),
        )
        return data if isinstance(data, list) else []

    def fetch_top_trader_ratio(self, symbol: str) -> Optional[float]:
        data = self.fetch_top_trader_position_ratio(symbol, period="1h", limit=1)
        row = data[-1] if data else None
        if isinstance(row, dict):
            value = self._number(row.get("longShortRatio"), -1.0)
            return value if value > 0 else None
        return None

    def _load_spec(self, inst_id: str) -> Optional[InstrumentSpec]:
        data = self._public_call("exchange_information")
        if not isinstance(data, dict):
            return None
        raw = next((row for row in (data.get("symbols") or [])
                    if row.get("symbol") == inst_id), None)
        if not raw:
            return None

        tick = step = min_qty = 0.0
        for item in raw.get("filters", []):
            if item.get("filterType") == "PRICE_FILTER":
                tick = self._number(item.get("tickSize"))
            elif item.get("filterType") == "LOT_SIZE":
                step = self._number(item.get("stepSize"))
                min_qty = self._number(item.get("minQty"))
        if tick <= 0 or step <= 0 or min_qty <= 0:
            return None
        return InstrumentSpec(
            venue="binance",
            inst_id=inst_id,
            base=self.canonical(inst_id),
            tick_size=tick,
            step_size=step,
            ct_val=1.0,
            min_size=min_qty,
            max_leverage=0.0,
            status=str(raw.get("status", "")).lower(),
            quantity_unit="base_asset",
            decimal_amount=False,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # Private account and order operations
    # ------------------------------------------------------------------
    def server_time_offset_ms(self, *, force: bool = False) -> float:
        """Read SDK-managed server time for diagnostics; SDK signs requests itself."""
        now = time.time()
        cached = self._SERVER_TIME_CACHE.get(self.base_url)
        if not force and cached and now - cached[0] < self.SERVER_TIME_TTL_S:
            return float(cached[1])
        try:
            data = self._public_call("check_server_time")
            server_ms = float((data or {}).get("serverTime"))
            offset = server_ms - now * 1000.0
        except (TypeError, ValueError):
            warnings.warn("[binance] SDK 服务器校时失败", RuntimeWarning)
            return 0.0
        self._SERVER_TIME_CACHE[self.base_url] = (now, offset)
        if abs(offset) >= self.SERVER_TIME_WARN_MS:
            warnings.warn(
                f"[binance] 本机时钟与 {self.base_url} 偏差 {offset:+.0f}ms，建议启用 NTP",
                RuntimeWarning,
            )
        return float(offset)

    def account_snapshot(self) -> Dict[str, Any]:
        data = self._private_call("account_information_v2")
        if not isinstance(data, dict):
            raise BinanceAPIError("bad_response", "account 返回结构异常")
        return {
            "venue": "binance",
            "currency": "USDT",
            "equity_usdt": self._number(data.get("totalMarginBalance") or data.get("totalWalletBalance")),
            "settled_equity_usdt": self._number(data.get("totalWalletBalance")),
            "available_usdt": self._number(data.get("availableBalance") or data.get("maxWithdrawAmount")),
            "position_margin": self._number(data.get("totalPositionInitialMargin") or data.get("totalInitialMargin")),
            "order_margin": self._number(data.get("totalOpenOrderInitialMargin")),
            "unrealized_pnl": self._number(data.get("totalUnrealizedProfit")),
            "can_trade": bool(data.get("canTrade", True)),
            "raw": data,
        }

    def fetch_income_history(
            self, *, symbol: Optional[str] = None, income_type: Optional[str] = None,
            start_time: Optional[int] = None, end_time: Optional[int] = None,
            limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return Binance income rows through the generated SDK endpoint."""
        income_enum = None
        if income_type:
            from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
                GetIncomeHistoryIncomeTypeEnum,
            )
            try:
                income_enum = GetIncomeHistoryIncomeTypeEnum(str(income_type).upper())
            except ValueError as exc:
                raise ValueError(f"Binance 不支持的资金流类型: {income_type}") from exc
        data = self._private_call(
            "get_income_history",
            symbol=self.native_symbol(symbol) if symbol else None,
            income_type=income_enum,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return data if isinstance(data, list) else []

    def fetch_account_trades(
            self, symbol: str, *, order_id: Optional[int] = None,
            start_time: Optional[int] = None, end_time: Optional[int] = None,
            from_id: Optional[int] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return account fills through the generated SDK endpoint."""
        data = self._private_call(
            "account_trade_list",
            symbol=self.native_symbol(symbol),
            order_id=order_id,
            start_time=start_time,
            end_time=end_time,
            from_id=from_id,
            limit=limit,
        )
        return data if isinstance(data, list) else []

    def fetch_position_risk(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return raw position-risk rows through the generated SDK endpoint."""
        data = self._private_call(
            "position_information_v2",
            symbol=self.native_symbol(symbol) if symbol else None,
        )
        return data if isinstance(data, list) else []

    def positions(self) -> List[Dict[str, Any]]:
        data = self._private_call("position_information_v2")
        rows = data if isinstance(data, list) else []
        out = []
        for position in rows:
            if not isinstance(position, dict):
                continue
            amount = self._number(position.get("positionAmt"))
            if abs(amount) < 1e-12:
                continue
            symbol = str(position.get("symbol") or "")
            out.append({
                "venue": "binance",
                "inst_id": symbol,
                "base": self.canonical(symbol),
                "side": "long" if amount > 0 else "short",
                "size_signed": amount,
                "entry_price": self._number(position.get("entryPrice")),
                "mark_price": self._number(position.get("markPrice")),
                "leverage": self._number(position.get("leverage")),
                "margin": self._number(position.get("isolatedMargin") or position.get("positionInitialMargin")),
                "notional": self._number(position.get("notional")),
                "margin_mode": str(position.get("marginType") or "cross").lower(),
                "unrealized_pnl": self._number(position.get("unRealizedProfit")),
                "liq_price": self._number(position.get("liquidationPrice")) or None,
                "raw": position,
            })
        return out

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        data = self._private_call(
            "current_all_open_orders",
            symbol=self.native_symbol(symbol) if symbol else None,
        )
        rows = data if isinstance(data, list) else []
        return [
            {
                "venue": "binance",
                "order_id": str(order.get("orderId") or ""),
                "client_order_id": str(order.get("clientOrderId") or ""),
                "inst_id": str(order.get("symbol") or ""),
                "base": self.canonical(str(order.get("symbol") or "")),
                "side": str(order.get("side") or "").lower(),
                "price": self._number(order.get("price")),
                "size": self._number(order.get("origQty")),
                "status": str(order.get("status") or ""),
                "raw": order,
            }
            for order in rows
            if isinstance(order, dict)
        ]

    def detect_position_mode(self) -> str:
        try:
            return interpret_dual_side_position(
                self._private_call("get_current_position_mode")
            )
        except Exception:
            return "unknown"

    def place_order(self, symbol: str, side: str, contracts: float,
                    price: Optional[float] = None, tif: str = "gtc",
                    text: str = "", position_side: Optional[str] = None,
                    reduce_only: bool = False) -> Dict[str, Any]:
        self._keys()
        inst = self.native_symbol(symbol)
        sdk_side, sdk_type = self._order_enums(side, price)
        qty = float(contracts)
        if qty <= 0:
            raise ValueError(f"下单数量必须为正数，收到: {contracts}")
        spec = self.fetch_instrument_spec(symbol)
        params = build_order_params(
            inst=inst, position_side=position_side, price=price, qty=qty,
            reduce_only=reduce_only, s=sdk_side.value, spec=spec,
            text=text, tif=tif,
        )
        try:
            checked = validate_entry_limit_notional(params=params, spec=spec)
        except ValueError as exc:
            warnings.warn(f"[binance] {exc}", RuntimeWarning)
            raise BinanceAPIError("local_min_notional", str(exc)) from exc
        if checked["minimum"] > 0:
            print(f"[binance] 限价开仓名义额预检通过：{checked['notional']} USDT ≥ 最低门槛 {checked['minimum']} USDT")

        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
            NewOrderReduceOnlyEnum,
            NewOrderTimeInForceEnum,
        )

        kwargs: Dict[str, Any] = {
            "symbol": inst,
            "side": sdk_side,
            "type": sdk_type,
            "quantity": float(params["quantity"]),
        }
        if params["type"] == "LIMIT":
            kwargs["time_in_force"] = NewOrderTimeInForceEnum[str(params["timeInForce"]).upper()]
            kwargs["price"] = float(params["price"])
        if position_side:
            kwargs["position_side"] = str(position_side).upper()
        if reduce_only:
            kwargs["reduce_only"] = NewOrderReduceOnlyEnum.TRUE
        if text:
            kwargs["new_client_order_id"] = str(text).strip()
        data = self._private_call("new_order", **kwargs)
        if not isinstance(data, dict):
            raise BinanceAPIError("bad_response", "下单响应结构异常")
        order_id = str(data.get("orderId") or "")
        client_oid = str(data.get("clientOrderId") or "")
        return {
            "venue": "binance", "id": order_id, "order_id": order_id,
            "client_order_id": client_oid, "text": client_oid,
            "status": str(data.get("status") or ""), "symbol": inst,
            "side": str(data.get("side") or sdk_side.value).lower(),
            "price": self._number(data.get("price")),
            "origQty": self._number(data.get("origQty")),
            "executedQty": self._number(data.get("executedQty")), "raw": data,
        }

    @staticmethod
    def _order_enums(side: str, price: Optional[float]):
        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
            NewOrderSideEnum,
            NewOrderTypeEnum,
        )

        side_enum = NewOrderSideEnum.BUY if str(side).lower() in ("long", "buy") else NewOrderSideEnum.SELL
        type_enum = NewOrderTypeEnum.LIMIT if price is not None and float(price) > 0 else NewOrderTypeEnum.MARKET
        return side_enum, type_enum

    def create_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.place_order(*args, **kwargs)

    def cancel_order(self, symbol: str, order_id: Optional[str] = None,
                     client_order_id: Optional[str] = None) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"symbol": self.native_symbol(symbol)}
        if order_id is not None:
            value = str(order_id)
            if value.isdigit():
                kwargs["order_id"] = int(value)
            else:
                kwargs["orig_client_order_id"] = value
        elif client_order_id:
            kwargs["orig_client_order_id"] = str(client_order_id)
        else:
            raise ValueError("撤单需 order_id 或 client_order_id")
        data = self._private_call("cancel_order", **kwargs)
        data = data if isinstance(data, dict) else {}
        return {
            "venue": "binance",
            "order_id": str(data.get("orderId") or ""),
            "status": str(data.get("status") or "UNKNOWN"),
            "raw": data,
        }

    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        inst = self.native_symbol(symbol)
        data = self._private_call("cancel_all_open_orders", symbol=inst)
        return {"venue": "binance", "symbol": inst, "result": data}

    def position_margin_type(self, inst: str) -> Optional[str]:
        try:
            rows = self._private_call("position_information_v2", symbol=inst)
        except Exception:
            return None
        row = rows[0] if isinstance(rows, list) and rows else rows
        if not isinstance(row, dict):
            return None
        value = str(row.get("marginType") or "").strip().lower()
        return value or None

    def set_leverage(self, symbol: str, leverage: float, margin_mode: str = "cross") -> Any:
        inst = self.native_symbol(symbol)
        want = str(margin_mode or "cross").strip().lower()
        have = self.position_margin_type(inst)
        if have is None:
            warnings.warn(f"[binance] {inst} 保证金档读回失败，将直接尝试设置 {want}", RuntimeWarning)
        elif have == want:
            return self._change_leverage(inst, leverage)

        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
            ChangeMarginTypeMarginTypeEnum,
        )

        try:
            self._private_call(
                "change_margin_type",
                symbol=inst,
                margin_type=(ChangeMarginTypeMarginTypeEnum.CROSSED if want == "cross"
                             else ChangeMarginTypeMarginTypeEnum.ISOLATED),
            )
        except BinanceAPIError as exc:
            if exc.code == -4046:
                pass
            else:
                after = self.position_margin_type(inst)
                if after == want:
                    warnings.warn(f"[binance] 保证金档写入返回 {exc.code}，但读回已={after}", RuntimeWarning)
                elif after is None and exc.code in (-1102, -1022):
                    warnings.warn(f"[binance] 保证金端点不可用({exc.code})且档位读不回，按现状继续", RuntimeWarning)
                else:
                    raise
        return self._change_leverage(inst, leverage)

    def _change_leverage(self, inst: str, leverage: float) -> Any:
        return self._private_call("change_initial_leverage", symbol=inst, leverage=int(leverage))

    def _send_algo_order(self, *, inst: str, side: str, position_side: Optional[str],
                         qty_str: Optional[str], trigger_price: Optional[float],
                         type_: str, working_type: str, client_algo_id: str) -> str:
        if trigger_price is None or float(trigger_price) <= 0:
            return ""
        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import (
            NewAlgoOrderAlgoTypeEnum,
            NewAlgoOrderClosePositionEnum,
            NewAlgoOrderReduceOnlyEnum,
            NewAlgoOrderSideEnum,
            NewAlgoOrderTypeEnum,
            NewAlgoOrderWorkingTypeEnum,
        )

        kwargs: Dict[str, Any] = {
            "algo_type": NewAlgoOrderAlgoTypeEnum.CONDITIONAL,
            "symbol": inst,
            "side": NewAlgoOrderSideEnum[side],
            "type": NewAlgoOrderTypeEnum[type_],
            "trigger_price": float(trigger_price),
            "working_type": NewAlgoOrderWorkingTypeEnum[working_type],
            "client_algo_id": client_algo_id,
        }
        if position_side:
            kwargs["position_side"] = str(position_side).upper()
        if qty_str:
            kwargs["quantity"] = float(qty_str)
            kwargs["reduce_only"] = NewAlgoOrderReduceOnlyEnum.TRUE
        else:
            kwargs["close_position"] = NewAlgoOrderClosePositionEnum.TRUE
        data = self._private_call("new_algo_order", **kwargs)
        return str((data or {}).get("algoId") or (data or {}).get("orderId") or "")

    def attach_protective_orders(self, symbol: str, side: str,
                                 tp_px: Optional[float] = None,
                                 sl_px: Optional[float] = None,
                                 working_type: str = "CONTRACT_PRICE",
                                 position_side: Optional[str] = None,
                                 expiration: Optional[int] = None,
                                 contracts: Optional[float] = None,
                                 **kwargs: Any) -> Dict[str, str]:
        inst = self.native_symbol(symbol)
        opp_side = "SELL" if str(side).lower() in ("long", "buy") else "BUY"
        wt = str(working_type or "").strip().upper()
        if wt not in ("MARK_PRICE", "CONTRACT_PRICE"):
            raise ValueError(f"workingType 非法: {working_type!r}")
        qty_val = float(contracts or kwargs.get("size") or 0.0)
        spec = self.fetch_instrument_spec(symbol)
        step = Decimal(str(spec.step_size if spec else 1e-6))
        qty_str = None
        if qty_val > 0:
            qty_dec = (Decimal(str(qty_val)) / step).to_integral_value(rounding=ROUND_DOWN) * step
            rendered = format(qty_dec, "f")
            qty_str = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
        return {
            "tp": self._send_algo_order(
                inst=inst, side=opp_side, position_side=position_side,
                qty_str=qty_str, trigger_price=tp_px, type_="TAKE_PROFIT_MARKET",
                working_type=wt, client_algo_id=f"t-astratp{uuid.uuid4().hex[:16]}",
            ),
            "sl": self._send_algo_order(
                inst=inst, side=opp_side, position_side=position_side,
                qty_str=qty_str, trigger_price=sl_px, type_="STOP_MARKET",
                working_type=wt, client_algo_id=f"t-astrasl{uuid.uuid4().hex[:16]}",
            ),
        }

    def list_protective_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        data = self._private_call(
            "current_all_algo_open_orders",
            symbol=self.native_symbol(symbol) if symbol else None,
        )
        rows = data if isinstance(data, list) else []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            algo_id = str(row.get("algoId") or row.get("orderId") or "")
            out.append({
                "id": algo_id,
                "algo_id": algo_id,
                "symbol": str(row.get("symbol") or ""),
                "side": str(row.get("side") or "").lower(),
                "trigger_price": self._number(row.get("triggerPrice")),
                "type": str(row.get("algoType") or row.get("type") or ""),
                "raw": row,
            })
        return out

    def query_algo_order(self, *, algo_id=None, client_algo_id=None) -> Any:
        kwargs = {}
        if algo_id is not None:
            kwargs["algo_id"] = int(algo_id)
        if client_algo_id:
            kwargs["client_algo_id"] = str(client_algo_id)
        if not kwargs:
            raise ValueError("查询条件单需 algoId 或 clientAlgoId")
        return self._private_call("query_algo_order", **kwargs)

    def current_all_algo_open_orders(self, *, symbol: Optional[str] = None) -> Any:
        return self._private_call(
            "current_all_algo_open_orders",
            symbol=self.native_symbol(symbol) if symbol else None,
        )

    def cancel_algo_order(self, *, algo_id=None, client_algo_id=None) -> Any:
        kwargs = {}
        if algo_id is not None:
            kwargs["algo_id"] = int(algo_id)
        if client_algo_id:
            kwargs["client_algo_id"] = str(client_algo_id)
        if not kwargs:
            raise ValueError("撤销条件单需 algoId 或 clientAlgoId")
        return self._private_call("cancel_algo_order", **kwargs)

    def cancel_protective_orders(self, symbol: str) -> Any:
        return self.cancel_all_algo_open_orders(symbol=symbol)

    def cancel_all_algo_open_orders(self, *, symbol: str) -> Any:
        return self._private_call(
            "cancel_all_algo_open_orders", symbol=self.native_symbol(symbol)
        )

    def fast_close_position(self, symbol: str, pos_side: Optional[str] = None) -> Dict[str, Any]:
        inst = self.native_symbol(symbol)
        rows = [p for p in self.positions() if p.get("inst_id") == inst]
        want = str(pos_side or "").strip().lower()
        if want in ("long", "short"):
            rows = [p for p in rows if str(p.get("side", "")).lower() == want]
        if not rows:
            return {"venue": "binance", "symbol": inst, "closed": False, "reason": "无持仓"}
        if len(rows) > 1:
            return {"venue": "binance", "symbol": inst, "closed": False,
                    "reason": "对冲模式双向同存，拒绝盲平——须指定 pos_side"}
        target = rows[0]
        size = abs(float(target.get("size_signed") or 0.0))
        if size <= 1e-12:
            return {"venue": "binance", "symbol": inst, "closed": False, "reason": "持仓为0"}
        close_side = "SELL" if str(target.get("side", "")).lower() == "long" else "BUY"
        raw = target.get("raw") if isinstance(target.get("raw"), dict) else {}
        position_side = str(raw.get("positionSide") or "").upper()
        if position_side in ("LONG", "SHORT"):
            return self.place_order(symbol, close_side, size, position_side=position_side)
        return self.place_order(symbol, close_side, size, reduce_only=True)

    @staticmethod
    def merged_protection_view(normal_open: List[Dict[str, Any]],
                               algo_open: List[Dict[str, Any]]) -> Dict[str, Any]:
        def normalize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            result = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                for key in ("orderId", "algoId", "origClientOrderId", "clientAlgoId", "clientOrderId"):
                    if item.get(key) is not None:
                        item[key] = str(item[key])
                result.append(item)
            return result

        normal = normalize(normal_open)
        conditional = normalize(algo_open)
        return {
            "entries": normal,
            "conditional": conditional,
            "protection_total": len(conditional),
            "open_orders_only": False,
        }
