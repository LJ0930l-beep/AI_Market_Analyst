"""Gate.io v4 private account adapter.

Supports:
1. Real account trade history retrieval (fetch_my_trades)
2. Real account positions, balance, and margin tracking
3. Explicit TestNet order execution with a release-locked Live adapter.
"""

from datetime import datetime, timezone
import logging
import math
import time
from typing import Optional, Dict, Any, List

logger = logging.getLogger("gate_live_client")

GATE_TESTNET_API_BASE_URL = "https://api-testnet.gateapi.io/api/v4"
GATE_LIVE_API_BASE_URL = "https://api.gateio.ws/api/v4"


def _error_text(exc: BaseException) -> str:
    """Return a bounded, credential-redacted diagnostic string."""
    value = str(exc or "").replace("\r", " ").replace("\n", " ").strip()
    return value[:240] or type(exc).__name__


def _map_gate_error(exc: BaseException) -> Dict[str, str]:
    """Map ccxt/network failures to stable machine codes and Chinese action text.

    ccxt exception types vary slightly by release, so the mapping intentionally
    uses both the class name and a bounded message.  No raw provider body is
    returned to the UI; the diagnostic is only a short, redacted hint.
    """
    name = type(exc).__name__.lower()
    message = _error_text(exc).lower()
    if "nonce" in name or "timestamp" in message or "clock" in message or "recvwindow" in message:
        return {"code": "GATE_TIME_SYNC_REQUIRED", "message_zh": "Gate 时间偏差或时间戳校验失败，请校准系统时间后重试。"}
    if "auth" in name or "permission" in name or "invalid key" in message or "api key" in message or "signature" in message or "key" in message and "invalid" in message:
        if "permission" in name or "permission" in message or "scope" in message or "read" in message and "denied" in message:
            return {"code": "GATE_PERMISSION_DENIED", "message_zh": "Gate API 凭证权限不足，请为该环境开启只读账户与合约权限。"}
        return {"code": "GATE_SIGNATURE_INVALID", "message_zh": "Gate API 签名或凭证无效，请确认 Key 属于当前 TestNet/Live 环境。"}
    if "timeout" in name or "timed out" in message or "deadline" in message:
        return {"code": "GATE_NETWORK_TIMEOUT", "message_zh": "Gate 请求超时，未确认账户状态；请稍后重试。"}
    if "network" in name or "connection" in message or "dns" in message or "tls" in message or "ssl" in message or "unreachable" in message:
        return {"code": "GATE_NETWORK_UNAVAILABLE", "message_zh": "无法连接 Gate 当前环境，请检查网络、DNS 或 TLS 后重试。"}
    if "badrequest" in name or "environment" in message or "testnet" in message and "live" in message:
        return {"code": "GATE_ENVIRONMENT_MISMATCH", "message_zh": "Gate 凭证与请求环境不匹配；TestNet 与 Live Key 不能互用。"}
    return {"code": "GATE_REMOTE_ERROR", "message_zh": "Gate 返回了未识别的错误，请查看环境、权限和接口类型后重试。"}


def _validation_failure(exc: BaseException, *, environment: str, endpoint: str) -> Dict[str, Any]:
    mapped = _map_gate_error(exc)
    return {
        "valid": False,
        "status": "VERIFICATION_FAILED",
        "code": mapped["code"],
        "message_zh": mapped["message_zh"],
        "reason": f"{mapped['code']}: {_error_text(exc)}",
        "api_environment": environment,
        "endpoint": endpoint,
    }


def _optional_float(value: Any) -> Optional[float]:
    """Parse an observed numeric field without turning absence into zero."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class GateLiveTrader:
    """Client for Gate.io private API and quantitative order execution."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        testnet: bool = False,
        live_trading_enabled: bool = False,
        api_base_url: Optional[str] = None,
        exchange: Any | None = None,
    ):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.testnet = testnet
        self.live_trading_enabled = live_trading_enabled
        default_endpoint = GATE_TESTNET_API_BASE_URL if testnet else GATE_LIVE_API_BASE_URL
        self.api_base_url = (api_base_url or default_endpoint).strip().rstrip("/")
        self.api_environment = "TESTNET" if testnet else "LIVE"
        # A concrete exchange can only be injected by an isolated caller
        # (tests/dry-run harness).  Production always builds the native CCXT
        # Gate adapter below so its nested endpoint map and sandbox switch
        # remain authoritative.
        self._exchange = exchange
        self.last_positions_status = "NOT_CHECKED"
        self.last_trades_status = "NOT_CHECKED"
        self.last_positions_error_code: Optional[str] = None
        self.last_trades_error_code: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _get_exchange(self):
        if self._exchange is not None:
            return self._exchange
        if not self.is_configured:
            raise ValueError("Gate.io API credentials not configured.")
        import ccxt

        config = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "timeout": 8000,
            "options": {
                "defaultType": "swap",
            },
        }
        endpoint = self.api_base_url
        # Reject an explicitly known Gate host from the other environment
        # before ccxt can sign a private request.  Custom endpoints remain
        # usable for deterministic tests and controlled gateways.
        endpoint_lower = endpoint.lower()
        if self.testnet and "api.gateio.ws" in endpoint_lower and "api-testnet.gateapi.io" not in endpoint_lower:
            raise ValueError("GATE_ENVIRONMENT_MISMATCH_TESTNET_ENDPOINT")
        if not self.testnet and "api-testnet.gateapi.io" in endpoint_lower:
            raise ValueError("GATE_ENVIRONMENT_MISMATCH_LIVE_ENDPOINT")
        if endpoint not in {GATE_TESTNET_API_BASE_URL, GATE_LIVE_API_BASE_URL}:
            # A flattened/custom URL map is unsafe with Gate's CCXT adapter:
            # it removes the futures/wallet route map and can sign a request
            # for the wrong environment.  Custom transports must be injected
            # explicitly by an isolated test harness instead.
            raise ValueError("GATE_CUSTOM_ENDPOINT_REQUIRES_EXPLICIT_EXCHANGE")
        self._exchange = ccxt.gate(config)
        if self.testnet:
            sandbox_switch = getattr(self._exchange, "set_sandbox_mode", None)
            if not callable(sandbox_switch):
                raise ValueError("GATE_CCXT_SANDBOX_UNSUPPORTED")
            sandbox_switch(True)
        self._assert_exchange_routing(self._exchange)
        return self._exchange

    def _assert_exchange_routing(self, exchange: Any) -> None:
        """Fail closed unless CCXT retained Gate's nested futures routes."""

        urls = getattr(exchange, "urls", None)
        api = urls.get("api") if isinstance(urls, dict) else None
        if not isinstance(api, dict):
            raise ValueError("GATE_CCXT_ENDPOINT_MAP_INVALID")
        values: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str):
                values.append(value.lower())
            elif isinstance(value, dict):
                for nested in value.values():
                    collect(nested)

        collect(api)
        expected_host = "api-testnet.gateapi.io" if self.testnet else "api.gateio.ws"
        if not any(expected_host in value for value in values):
            raise ValueError("GATE_CCXT_ENDPOINT_ENVIRONMENT_MISMATCH")
        if not isinstance(api.get("public"), dict) or not isinstance(api.get("private"), dict):
            raise ValueError("GATE_CCXT_ENDPOINT_MAP_INVALID")
        if not isinstance(api["public"].get("futures"), str) or not isinstance(api["private"].get("futures"), str):
            raise ValueError("GATE_CCXT_FUTURES_ROUTE_MISSING")

    @staticmethod
    def _exchange_symbol(exchange: Any, symbol: str) -> str:
        """Resolve an app symbol to the CCXT linear-swap symbol.

        The application uses stable identifiers such as ``BTCUSDT`` and
        ``BTC_USDT``.  Gate/CCXT private endpoints conventionally expect
        ``BTC/USDT:USDT``.  Prefer loaded exchange metadata when available;
        the deterministic fallback keeps mocked adapters and offline tests
        usable without pretending metadata was observed.
        """
        raw = str(symbol or "").strip().upper()
        if not raw:
            raise ValueError("GATE_SYMBOL_REQUIRED")
        if "/" in raw:
            return raw
        compact = raw.replace("_", "").replace("-", "")
        try:
            markets = exchange.load_markets()
        except Exception:
            markets = {}
        if isinstance(markets, dict):
            matches = []
            for market in markets.values():
                if not isinstance(market, dict):
                    continue
                if not (market.get("swap") and market.get("linear") and str(market.get("settle") or "").upper() == "USDT"):
                    continue
                candidates = {
                    str(market.get("symbol") or "").upper(),
                    str(market.get("id") or "").upper(),
                    f"{str(market.get('base') or '').upper()}{str(market.get('quote') or '').upper()}",
                }
                if compact in {candidate.replace("/", "").replace(":USDT", "").replace("_", "") for candidate in candidates}:
                    matches.append(str(market.get("symbol") or ""))
            if len(matches) == 1 and matches[0]:
                return matches[0]
        if compact.endswith("USDT") and len(compact) > 4:
            return f"{compact[:-4]}/USDT:USDT"
        return raw

    def validate_credentials(self) -> Dict[str, Any]:
        """Verify the current environment with a read-only futures balance call."""
        endpoint = self.api_base_url
        if not self.is_configured:
            return {
                "valid": False,
                "status": "NOT_CONFIGURED",
                "code": "GATE_CREDENTIALS_REQUIRED",
                "message_zh": "请填写当前 Gate TestNet/Live 环境对应的 API Key 和 Secret。",
                "api_environment": self.api_environment,
                "endpoint": endpoint,
            }
        try:
            ex = self._get_exchange()
            balance = ex.fetch_balance()
            if not isinstance(balance, dict):
                return {
                    "valid": False,
                    "status": "VERIFICATION_FAILED",
                    "code": "GATE_RESPONSE_SCHEMA_INVALID",
                    "message_zh": "Gate 余额响应格式异常，未保存新凭证。",
                    "api_environment": self.api_environment,
                    "endpoint": endpoint,
                }
            usdt_info = balance.get("USDT", {})
            if not isinstance(usdt_info, dict):
                return {
                    "valid": False,
                    "status": "VERIFICATION_FAILED",
                    "code": "GATE_BALANCE_SCHEMA_INVALID",
                    "message_zh": "Gate 余额响应缺少 USDT 账户对象，未保存新凭证。",
                    "api_environment": self.api_environment,
                    "endpoint": endpoint,
                }
            values = {key: _optional_float(usdt_info.get(key)) for key in ("total", "free", "used")}
            return {
                "valid": True,
                "status": "VERIFIED_READ_ONLY",
                "code": "GATE_CREDENTIALS_VERIFIED",
                "message_zh": f"Gate {self.api_environment} 只读账户验证成功。",
                "account_type": "Gate.io Futures / Swap",
                "data_status": "AVAILABLE" if all(value is not None for value in values.values()) else "DEGRADED",
                "total_usdt": values["total"],
                "free_usdt": values["free"],
                "used_usdt": values["used"],
                "api_environment": self.api_environment,
                "endpoint": endpoint,
            }
        except Exception as exc:
            return _validation_failure(exc, environment=self.api_environment, endpoint=endpoint)

    def get_account_balance(self) -> Dict[str, Any]:
        """Retrieve real futures USDT balance."""
        if not self.is_configured:
            return {
                "configured": False,
                "data_status": "NOT_CONFIGURED",
                "observed_at": None,
                "total": None,
                "free": None,
                "used": None,
            }
        try:
            ex = self._get_exchange()
            balance = ex.fetch_balance()
            if not isinstance(balance, dict):
                raise ValueError("GATE_RESPONSE_SCHEMA_INVALID")
            usdt = balance.get("USDT", {})
            observed_at = datetime.now(timezone.utc).isoformat()
            if not isinstance(usdt, dict):
                raise ValueError("GATE_BALANCE_SCHEMA_INVALID")
            values = {key: _optional_float(usdt.get(key)) for key in ("total", "free", "used")}
            return {
                "configured": True,
                "data_status": "AVAILABLE" if all(value is not None for value in values.values()) else "DEGRADED",
                "observed_at": observed_at,
                **({} if all(value is not None for value in values.values()) else {"error_code": "GATE_BALANCE_FIELDS_MISSING"}),
                **values,
            }
        except Exception as exc:
            mapped = _map_gate_error(exc)
            logger.warning("Failed to fetch Gate %s balance (%s)", self.api_environment, mapped["code"])
            return {
                "configured": True,
                "data_status": "UNAVAILABLE",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "error_code": mapped["code"],
                "message_zh": mapped["message_zh"],
                "total": None,
                "free": None,
                "used": None,
            }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Retrieve real open futures positions."""
        if not self.is_configured:
            self.last_positions_status = "NOT_CONFIGURED"
            self.last_positions_error_code = "GATE_CREDENTIALS_REQUIRED"
            return []
        self.last_positions_error_code = None
        try:
            ex = self._get_exchange()
            raw_positions = ex.fetch_positions()
            if not isinstance(raw_positions, list):
                raise ValueError("GATE_RESPONSE_SCHEMA_INVALID")
            results = []
            degraded = False
            for p in raw_positions:
                if not isinstance(p, dict):
                    degraded = True
                    continue
                raw_size = p.get("contracts", p.get("size"))
                size = _optional_float(raw_size)
                if size is None:
                    degraded = True
                    results.append({
                        "symbol": p.get("symbol"),
                        "side": str(p.get("side") or "UNKNOWN").upper(),
                        "contracts": None,
                        "position_status": "UNKNOWN_SIZE",
                    })
                    continue
                if abs(size) > 0:
                    results.append({
                        "symbol": p.get("symbol"),
                        "side": str(p.get("side") or "UNKNOWN").upper(),
                        "contracts": size,
                        "entry_price": _optional_float(p.get("entryPrice")),
                        "mark_price": _optional_float(p.get("markPrice")),
                        "unrealized_pnl": _optional_float(p.get("unrealizedPnl")),
                        "leverage": _optional_float(p.get("leverage")),
                        "liquidation_price": _optional_float(p.get("liquidationPrice")),
                        "initial_margin": _optional_float(p.get("initialMargin")),
                    })
            self.last_positions_status = "DEGRADED" if degraded else "AVAILABLE"
            return results
        except Exception as exc:
            mapped = _map_gate_error(exc)
            logger.warning("Failed to fetch Gate %s positions (%s)", self.api_environment, mapped["code"])
            self.last_positions_status = "UNAVAILABLE"
            self.last_positions_error_code = mapped["code"]
            return []

    def get_trades(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve real account filled trade history."""
        if not self.is_configured:
            self.last_trades_status = "NOT_CONFIGURED"
            self.last_trades_error_code = "GATE_CREDENTIALS_REQUIRED"
            return []
        self.last_trades_error_code = None
        try:
            ex = self._get_exchange()
            target_symbol = self._exchange_symbol(ex, symbol) if symbol else None
            
            raw_trades = ex.fetch_my_trades(target_symbol, limit=max(1, min(limit, 100)))
            if not isinstance(raw_trades, list):
                raise ValueError("GATE_TRADES_RESPONSE_SCHEMA_INVALID")
            results = []
            degraded = False
            for t in raw_trades:
                if not isinstance(t, dict):
                    degraded = True
                    continue
                fee = t.get("fee") or {}
                fee_cost = _optional_float(fee.get("cost")) if isinstance(fee, dict) else None
                if fee_cost is None:
                    degraded = True
                results.append({
                    "id": t.get("id"),
                    "order_id": t.get("order"),
                    "symbol": str(t.get("symbol") or "").replace(":USDT", "").replace("/", "") or None,
                    "timestamp": t.get("timestamp"),
                    "datetime": t.get("datetime") or (datetime.fromtimestamp(t["timestamp"] / 1000, timezone.utc).isoformat() if t.get("timestamp") else None),
                    "side": str(t.get("side") or "UNKNOWN").upper(),
                    "price": _optional_float(t.get("price")),
                    "amount": _optional_float(t.get("amount")),
                    "cost": _optional_float(t.get("cost")),
                    "fee_cost": fee_cost,
                    "fee_currency": fee.get("currency") if isinstance(fee, dict) else None,
                    "fee_evidence_status": "OBSERVED_REMOTE" if fee_cost is not None else "UNKNOWN_NOT_PROVIDED",
                })
            self.last_trades_status = "DEGRADED" if degraded else "AVAILABLE"
            return sorted(results, key=lambda x: x.get("timestamp") or 0, reverse=True)
        except Exception as exc:
            mapped = _map_gate_error(exc)
            logger.warning("Failed to fetch Gate %s trades (%s)", self.api_environment, mapped["code"])
            self.last_trades_status = "UNAVAILABLE"
            self.last_trades_error_code = mapped["code"]
            return []

    def set_leverage(self, symbol: str, leverage: Optional[int] = None) -> Dict[str, Any]:
        """Set position leverage on Gate.io futures."""
        if leverage is None or not isinstance(leverage, int) or leverage < 1 or leverage > 100:
            return {"acknowledged": False, "error": "LEVERAGE_REQUIRED: explicit leverage in [1, 100] is required", "symbol": symbol, "leverage": None}
        if not self.live_trading_enabled:
            return {"acknowledged": True, "dry_run": True, "symbol": symbol, "leverage": leverage}
        try:
            ex = self._get_exchange()
            exchange_symbol = self._exchange_symbol(ex, symbol)
            res = ex.set_leverage(leverage, exchange_symbol)
            return {"acknowledged": True, "dry_run": False, "symbol": symbol, "leverage": leverage, "result": res}
        except Exception as exc:
            mapped = _map_gate_error(exc)
            return {"acknowledged": False, "error_code": mapped["code"], "message_zh": mapped["message_zh"], "symbol": symbol, "leverage": leverage}

    def place_order(
        self,
        symbol: str,
        side: str,  # 'LONG' or 'SHORT' / 'BUY' or 'SELL'
        amount: float,
        price: Optional[float] = None,
        order_type: str = "market",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        leverage: Optional[int] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit to the configured Gate environment or validate without sending.

        ``live_trading_enabled`` means the adapter is allowed to send to its
        explicitly configured environment.  The account factory enables it
        for TestNet and keeps it disabled for Live; a disabled adapter is
        validation-only and never fabricates a fill.
        """
        side_clean = side.upper()
        ccxt_side = "buy" if side_clean in {"LONG", "BUY"} else "sell"
        order_type_clean = str(order_type or "").lower().strip()
        if order_type_clean not in {"market", "limit"}:
            return {"status": "REJECTED", "error_code": "GATE_ORDER_TYPE_UNSUPPORTED", "message_zh": "Gate 仅支持明确的市价或限价订单。", "symbol": symbol}
        if order_type_clean == "limit" and _optional_float(price) is None:
            return {"status": "REJECTED", "error_code": "LIMIT_PRICE_REQUIRED", "message_zh": "限价订单必须提供有效限价。", "symbol": symbol}
        amount_value = _optional_float(amount)
        if amount_value is None or amount_value <= 0:
            return {"status": "REJECTED", "error_code": "GATE_AMOUNT_INVALID", "message_zh": "订单数量必须是有限正数。", "symbol": symbol}
        now_iso = datetime.now(timezone.utc).isoformat()

        if not self.live_trading_enabled:
            # Dry-run is a validation-only mode.  It never fabricates a fill.
            mock_id = client_order_id or f"gate_dry_{int(time.time() * 1000)}"
            return {
                "status": "DRY_RUN_ACKNOWLEDGED",
                "dry_run": True,
                "order_id": mock_id,
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side_clean,
                "type": order_type_clean,
                "amount": amount,
                "price": price or "MARKET_BEST",
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "protection_status": "VALIDATE_ONLY_NOT_SENT",
                "protection_orders": [],
                # Dry-run receipts must preserve the caller's fact.  A
                # missing leverage is UNKNOWN/None, never an implicit 100x
                # position that could be mistaken for an approved setting.
                "leverage": leverage,
                "created_at": now_iso,
                "message": "已通过 Gate 参数预审，未向交易所发送请求。",
            }

        # Real live execution
        try:
            ex = self._get_exchange()
            exchange_symbol = self._exchange_symbol(ex, symbol)
            if leverage:
                lev_res = self.set_leverage(exchange_symbol, leverage)
                if not lev_res.get("acknowledged", True):
                    logger.error("Failed to set leverage %sx for %s: %s", leverage, symbol, lev_res.get("error"))
                    return {
                        "status": "EXECUTION_FAILED",
                        "dry_run": False,
                        "error": f"LEVERAGE_CHANGE_FAILED: {lev_res.get('error_code') or lev_res.get('error') or 'GATE_LEVERAGE_REJECTED'}",
                        "error_code": lev_res.get("error_code") or "GATE_LEVERAGE_REJECTED",
                        "message_zh": lev_res.get("message_zh") or "Gate 杠杆设置失败，未提交订单。",
                        "symbol": symbol,
                        "created_at": now_iso,
                    }
            
            params: Dict[str, Any] = {}
            if client_order_id:
                # Gate futures calls the client correlation field ``text``;
                # keep the local intent id in a Gate-compatible namespace.
                params["text"] = client_order_id if str(client_order_id).startswith("t-") else f"t-{client_order_id}"
            if reduce_only:
                # CCXT's unified field is translated to Gate's native
                # ``reduce_only`` request field by gate.create_order_request.
                # Do not pass the native spelling here: passing both creates
                # ambiguous duplicate parameters in some CCXT releases.
                params["reduceOnly"] = True

            order = ex.create_order(
                symbol=exchange_symbol,
                type=order_type_clean,
                side=ccxt_side,
                amount=amount,
                price=price,
                params=params,
            )

            # Strict F01 Fix: A receipt with status 'open' and filled == 0 is ACKNOWLEDGED, NEVER LIVE_EXECUTED!
            if not isinstance(order, dict):
                raise ValueError("GATE_RESPONSE_SCHEMA_INVALID")
            raw_status = str(order.get("status") or "").lower()
            filled = _optional_float(order.get("filled")) or 0.0
            order_amount = _optional_float(order.get("amount")) or float(amount)

            if filled == 0 or raw_status == "open":
                computed_status = "ACKNOWLEDGED"
            elif filled >= order_amount:
                computed_status = "FILLED"
            elif filled > 0:
                computed_status = "PARTIALLY_FILLED"
            else:
                computed_status = "ACKNOWLEDGED"

            fills = order.get("fills") if isinstance(order.get("fills"), list) else []
            response = {
                "status": computed_status,
                "dry_run": False,
                "order_id": str(order.get("id")),
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side_clean,
                "amount": amount,
                "filled": filled,
                "price": order.get("price"),
                "average": order.get("average"),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "fills": fills,
                "created_at": now_iso,
                "protection_status": "NOT_REQUESTED",
                "protection_orders": [],
            }
            if order.get("status") in {"canceled", "cancelled", "rejected", "expired"}:
                response["status"] = {"canceled": "CANCELED", "cancelled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED"}[str(order.get("status"))]
            if stop_loss is not None or take_profit is not None:
                if filled <= 0:
                    response["protection_status"] = "PENDING_ENTRY_FILL"
                else:
                    try:
                        protection = self._place_protection_orders(
                            ex,
                            exchange_symbol,
                            side=ccxt_side,
                            amount=filled,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            client_order_id=client_order_id,
                        )
                        response["protection_orders"] = protection
                        response["protection_status"] = "PROTECTED"
                    except Exception as protection_exc:
                        mapped_protection = _map_gate_error(protection_exc)
                        response.update(
                            {
                                "status": "PROTECTION_FAILED",
                                "protection_status": "PROTECTION_FAILED",
                                "error_code": "GATE_PROTECTION_FAILED",
                                "message_zh": "入场已成交但原生止损/止盈未确认，已阻断后续风险动作。",
                                "protection_error_code": mapped_protection["code"],
                                "protection_orders": [],
                            }
                        )
            return response
        except Exception as exc:
            mapped = _map_gate_error(exc)
            logger.error("Gate %s order execution failed (%s)", self.api_environment, mapped["code"])
            is_timeout = any(t in type(exc).__name__.lower() for t in ("timeout", "timedout")) or "timeout" in str(exc).lower()
            if is_timeout:
                hint = _error_text(exc)
                for secret in (self.api_key, self.api_secret):
                    if secret:
                        hint = hint.replace(secret, "[REDACTED]")
                return {
                    "status": "UNKNOWN",
                    "dry_run": False,
                    "order_id": "UNKNOWN",
                    "client_order_id": client_order_id,
                    "error_code": "GATE_NETWORK_TIMEOUT_UNKNOWN",
                    "message_zh": "Gate 下单请求超时，结果未知，必须先对账，禁止盲目重发。",
                    "error": f"ORDER_TIMEOUT: GATE_NETWORK_TIMEOUT_UNKNOWN: {hint}",
                    "symbol": symbol,
                    "created_at": now_iso,
                }
            return {
                "status": "REJECTED",
                "dry_run": False,
                "error_code": mapped["code"],
                "message_zh": mapped["message_zh"],
                "symbol": symbol,
                "created_at": now_iso,
            }

    @staticmethod
    def _place_protection_orders(
        exchange: Any,
        exchange_symbol: str,
        *,
        side: str,
        amount: float,
        stop_loss: float | None,
        take_profit: float | None,
        client_order_id: str | None,
    ) -> list[dict[str, Any]]:
        """Create separate Gate native trigger legs after an observed fill.

        Gate/CCXT accepts one conditional leg per ``create_order`` call.  The
        entry order therefore never carries stop/take-profit parameters.  A
        conditional response is only considered armed when it contains an
        exchange id and an active/open (or equivalent accepted) status.
        """

        if amount <= 0:
            raise ValueError("GATE_PROTECTION_AMOUNT_INVALID")
        protective_side = "sell" if side == "buy" else "buy"
        legs = (("stop_loss", stop_loss, "stopLossPrice"), ("take_profit", take_profit, "takeProfitPrice"))
        results: list[dict[str, Any]] = []
        for name, trigger_price, unified_key in legs:
            if trigger_price is None:
                continue
            try:
                trigger = float(trigger_price)
            except (TypeError, ValueError):
                raise ValueError(f"GATE_{name.upper()}_INVALID")
            if not math.isfinite(trigger) or trigger <= 0:
                raise ValueError(f"GATE_{name.upper()}_INVALID")
            params: dict[str, Any] = {
                unified_key: trigger,
                "reduceOnly": True,
                # Gate's trigger price type: 1 = mark price.
                "price_type": 1,
            }
            if client_order_id:
                suffix = "sl" if name == "stop_loss" else "tp"
                params["text"] = f"{client_order_id}-{suffix}"[:28]
            order = exchange.create_order(
                symbol=exchange_symbol,
                type="market",
                side=protective_side,
                amount=amount,
                price=None,
                params=params,
            )
            if not isinstance(order, dict):
                raise ValueError("GATE_PROTECTION_RESPONSE_SCHEMA_INVALID")
            order_id = order.get("id")
            info = order.get("info") if isinstance(order.get("info"), dict) else {}
            status = str(order.get("status") or info.get("status") or "").lower()
            if not order_id or status not in {"open", "active", "new", "accepted", "pending", "closed"}:
                raise ValueError("GATE_PROTECTION_NOT_CONFIRMED")
            results.append(
                {
                    "leg": name,
                    "order_id": str(order_id),
                    "status": status,
                    "side": protective_side.upper(),
                    "amount": amount,
                    "trigger_price": trigger,
                    "reduce_only": True,
                    "price_type": "MARK_PRICE",
                }
            )
        if not results:
            raise ValueError("GATE_PROTECTION_PLAN_EMPTY")
        return results

    def reconcile_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """Query actual status and fills from exchange to reconcile UNKNOWN orders."""
        if not self.live_trading_enabled:
            return {"status": "ACKNOWLEDGED", "filled": 0.0, "reconciled": True}
        try:
            ex = self._get_exchange()
            exchange_symbol = self._exchange_symbol(ex, symbol)
            order = ex.fetch_order(order_id, exchange_symbol)
            if not isinstance(order, dict):
                raise ValueError("GATE_RESPONSE_SCHEMA_INVALID")
            filled = _optional_float(order.get("filled")) or 0.0
            amount = _optional_float(order.get("amount")) or 0.0
            raw_status = str(order.get("status") or "").lower()
            if filled >= amount and amount > 0:
                final_status = "FILLED"
            elif filled > 0:
                final_status = "PARTIALLY_FILLED"
            elif raw_status in {"canceled", "cancelled", "expired", "rejected"}:
                final_status = "CANCELED"
            else:
                final_status = "ACKNOWLEDGED"

            return {
                "reconciled": True,
                "order_id": order_id,
                "symbol": symbol,
                "status": final_status,
                "filled": filled,
                "average": order.get("average"),
            }
        except Exception as exc:
            mapped = _map_gate_error(exc)
            logger.error("Failed to reconcile Gate %s order (%s)", order_id, mapped["code"])
            return {"reconciled": False, "error_code": mapped["code"], "message_zh": mapped["message_zh"]}

    def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """Return a normalized exchange order for the gateway reconciler."""
        if not self.live_trading_enabled:
            return {"order_id": order_id, "symbol": symbol, "status": "open", "filled": 0.0, "amount": 0.0}
        ex = self._get_exchange()
        exchange_symbol = self._exchange_symbol(ex, symbol)
        order = ex.fetch_order(order_id, exchange_symbol)
        if not isinstance(order, dict):
            raise ValueError("GATE_RESPONSE_SCHEMA_INVALID")
        return {
            "order_id": str(order.get("id") or order_id),
            "symbol": symbol,
            "status": str(order.get("status") or "unknown").lower(),
            "filled": _optional_float(order.get("filled")) or 0.0,
            "amount": _optional_float(order.get("amount")) or 0.0,
            "average": order.get("average"),
            "price": order.get("price"),
            "fee": order.get("fee"),
            "fills": order.get("fills") if isinstance(order.get("fills"), list) else [],
        }

    def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """Cancel an open order."""
        if not self.live_trading_enabled:
            return {"cancelled": True, "dry_run": True, "order_id": order_id, "symbol": symbol}
        try:
            ex = self._get_exchange()
            exchange_symbol = self._exchange_symbol(ex, symbol)
            res = ex.cancel_order(order_id, exchange_symbol)
            return {"cancelled": True, "dry_run": False, "status": "CANCELED", "order_id": order_id, "symbol": symbol, "result": res if isinstance(res, dict) else {}}
        except Exception as exc:
            mapped = _map_gate_error(exc)
            return {"cancelled": False, "status": "UNKNOWN", "error_code": mapped["code"], "message_zh": mapped["message_zh"]}

    def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Cancel all open orders for a symbol or account."""
        if not self.live_trading_enabled:
            return {"cancelled_all": True, "dry_run": True, "symbol": symbol}
        try:
            ex = self._get_exchange()
            exchange_symbol = self._exchange_symbol(ex, symbol) if symbol else None
            res = ex.cancel_all_orders(exchange_symbol)
            return {"cancelled_all": True, "dry_run": False, "result": res}
        except Exception as exc:
            mapped = _map_gate_error(exc)
            return {"cancelled_all": False, "error_code": mapped["code"], "message_zh": mapped["message_zh"]}

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Emergency market close of an active position."""
        if not self.live_trading_enabled:
            return {"closed": True, "dry_run": True, "symbol": symbol}
        try:
            ex = self._get_exchange()
            positions = self.get_positions()
            target = str(symbol or "").upper().replace("/", "").replace(":USDT", "").replace("_", "").replace("-", "")
            pos = next(
                (
                    p for p in positions
                    if target and target in str(p.get("symbol") or "").upper().replace("/", "").replace(":USDT", "").replace("_", "").replace("-", "")
                ),
                None,
            )
            if not pos:
                return {"closed": False, "reason": "No active position found for symbol"}
            
            close_side = "sell" if pos["side"] == "LONG" else "buy"
            exchange_symbol = self._exchange_symbol(ex, symbol)
            order = ex.create_order(
                symbol=exchange_symbol,
                type="market",
                side=close_side,
                amount=abs(pos["contracts"]),
                params={"reduceOnly": True},
            )
            return {"closed": True, "dry_run": False, "order": order}
        except Exception as exc:
            mapped = _map_gate_error(exc)
            return {"closed": False, "error_code": mapped["code"], "message_zh": mapped["message_zh"]}


def get_gate_credentials_from_store(store) -> Dict[str, Any]:
    """Retrieve saved Gate.io credentials through secure CredentialVault."""
    from core.security.credentials import CredentialVault
    meta = CredentialVault.get_metadata(store)
    k, s = CredentialVault.get_in_memory_keys(store)
    return {
        "configured": meta["configured"],
        "api_key": k or "",
        "api_secret": s or "",
        "api_key_masked": meta["api_key_masked"],
        "live_enabled": False,  # Hard gate: factory default locked
        "testnet": meta["testnet"],
        "migration_status": meta["migration_status"],
        "updated_at": meta["updated_at"],
    }


def save_gate_credentials_to_store(
    store,
    api_key: str,
    api_secret: str,
    live_enabled: bool = False,
    testnet: bool = False,
) -> Dict[str, Any]:
    """Persist Gate.io credentials safely into DPAPI CredentialVault."""
    from core.security.credentials import CredentialVault
    meta = CredentialVault.save_credentials(store, api_key, api_secret, testnet=testnet)
    return {
        "configured": meta["configured"],
        "api_key_masked": meta["api_key_masked"],
        "live_enabled": False,  # Hard gate: live_enabled is locked by release policy
        "testnet": meta["testnet"],
        "migration_status": meta["migration_status"],
        "updated_at": meta["updated_at"],
    }
