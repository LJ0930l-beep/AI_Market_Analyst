"""Gate.io v4 private account adapter.

Supports:
1. Real account trade history retrieval (fetch_my_trades)
2. Real account positions, balance, and margin tracking
3. Explicit TestNet and Live order execution through separately scoped
   adapters and endpoints.
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
        return {"code": "GATE_NETWORK_UNAVAILABLE", "message_zh": "无法连接 Gate 当前环境，请检查网络、代理配置、DNS 或 TLS 后重试。"}
    # Do not turn every Gate/CCXT 400 into an environment mismatch.  Gate
    # uses BadRequest for ordinary parameter errors too (for example an
    # overlong client ``text``), and that false diagnosis makes a valid
    # TestNet credential look unusable.
    if "environment" in message or "sandbox" in message or ("testnet" in message and "live" in message):
        return {"code": "GATE_ENVIRONMENT_MISMATCH", "message_zh": "Gate 凭证与请求环境不匹配；TestNet 与 Live Key 不能互用。"}
    if "badrequest" in name or "invalid_param" in message or "invalid parameter" in message:
        return {"code": "GATE_REMOTE_BAD_REQUEST", "message_zh": "Gate 拒绝了请求参数，请按远端合约规则修正后重试。"}
    return {"code": "GATE_REMOTE_ERROR", "message_zh": "Gate 返回了未识别的错误，请查看环境、权限和接口类型后重试。"}


def _execute_with_retry(fn, max_retries: int = 2, delay_seconds: float = 0.5):
    """Execute a callable with bounded exponential backoff on transient network faults."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            mapped = _map_gate_error(exc)
            if attempt < max_retries and mapped.get("code") in {
                "GATE_NETWORK_UNAVAILABLE",
                "GATE_NETWORK_TIMEOUT",
            }:
                time.sleep(delay_seconds * (2 ** attempt))
                continue
            raise
    if last_exc is not None:
        raise last_exc


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


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def _symbol_compact(value: Any) -> str:
    return str(value or "").upper().replace("/", "").replace(":USDT", "").replace("_", "").replace("-", "")


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
        self.last_open_orders_status = "NOT_CHECKED"
        self.last_positions_error_code: Optional[str] = None
        self.last_trades_error_code: Optional[str] = None
        self.last_open_orders_error_code: Optional[str] = None

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
            "timeout": 20000,
            "options": {
                "defaultType": "swap",
            },
        }
        import os
        proxy_env = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("all_proxy")
        )
        if proxy_env and str(proxy_env).strip():
            clean_proxy = str(proxy_env).strip()
            config["proxies"] = {
                "http": clean_proxy,
                "https": clean_proxy,
            }
            config["aiohttp_proxy"] = clean_proxy
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

    @staticmethod
    def _position_margin_mode(position: Any) -> Optional[str]:
        """Return Gate's unified margin mode without inferring one.

        Gate rejects a leverage request that silently switches an open
        position from cross to isolated (or the reverse).  CCXT defaults to
        isolated when no mode is supplied, therefore the adapter must use
        the mode observed from the remote position before it submits the
        mutable leverage request.
        """
        if not isinstance(position, dict):
            return None
        values = [position.get("marginMode")]
        raw = position.get("info")
        if isinstance(raw, dict):
            values.append(raw.get("margin_mode"))
            values.append(raw.get("mode"))
        for value in values:
            normalized = str(value or "").strip().lower().replace("_", "-")
            if normalized in {"cross", "cross-margin", "crossmargin"} or normalized.startswith("dual-"):
                return "cross"
            if normalized in {"isolated", "isolated-margin", "isolatedmargin"}:
                return "isolated"
        return None

    def _remote_margin_mode(self, exchange: Any, exchange_symbol: str) -> tuple[Optional[str], bool]:
        """Read the existing Gate position mode for a contract.

        Prefer a non-zero position because dual-mode Gate accounts can return
        an inactive long/short leg too.  A zero-size position still supplies
        an authoritative configured mode when no active leg exists.  Failure
        to observe this fact is deliberately not converted into a guessed
        mode: changing leverage must not also change an unknown margin mode.
        """
        fetch_positions = getattr(exchange, "fetch_positions", None)
        if not callable(fetch_positions):
            return None, False
        try:
            try:
                positions = fetch_positions([exchange_symbol])
            except TypeError:
                # Lightweight test adapters and older CCXT implementations
                # expose a no-argument variant.
                positions = fetch_positions()
        except Exception:
            return None, False
        if not isinstance(positions, list):
            return None, False
        fallback: Optional[str] = None
        for position in positions:
            if not isinstance(position, dict):
                continue
            candidate_symbol = str(position.get("symbol") or "")
            raw = position.get("info")
            if not candidate_symbol and isinstance(raw, dict):
                candidate_symbol = str(raw.get("contract") or "")
            if _symbol_compact(candidate_symbol) != _symbol_compact(exchange_symbol):
                continue
            mode = self._position_margin_mode(position)
            if mode is None:
                continue
            contracts = _optional_float(position.get("contracts"))
            if contracts is None and isinstance(raw, dict):
                contracts = _optional_float(raw.get("size"))
            if contracts is not None and contracts != 0:
                return mode, True
            fallback = fallback or mode
        return fallback, True

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
            normalized = self._normalize_futures_balance(balance, usdt_info)
            values = {
                "total": normalized.get("total"),
                "free": normalized.get("free"),
                "used": normalized.get("used"),
            }
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
            balance = _execute_with_retry(lambda: ex.fetch_balance())
            if not isinstance(balance, dict):
                raise ValueError("GATE_RESPONSE_SCHEMA_INVALID")
            usdt = balance.get("USDT", {})
            observed_at = datetime.now(timezone.utc).isoformat()
            if not isinstance(usdt, dict):
                raise ValueError("GATE_BALANCE_SCHEMA_INVALID")
            normalized = self._normalize_futures_balance(balance, usdt)
            total = normalized.get("total")
            free = normalized.get("free")
            used = normalized.get("used")
            unrealized = normalized.get("unrealized_pnl")
            realized = normalized.get("realized_pnl")
            equity = normalized.get("equity")
            values = {"total": total, "free": free, "used": used}
            return {
                "configured": True,
                "data_status": "AVAILABLE" if all(value is not None for value in values.values()) else "DEGRADED",
                "observed_at": observed_at,
                **({} if all(value is not None for value in values.values()) else {"error_code": "GATE_BALANCE_FIELDS_MISSING"}),
                **values,
                "equity": equity,
                "unrealized_pnl": unrealized,
                "realized_pnl": realized,
                "position_margin": normalized.get("position_margin"),
                "order_margin": normalized.get("order_margin"),
                "equity_basis": normalized.get("equity_basis"),
                "available_margin_basis": normalized.get("available_margin_basis"),
                "used_margin_basis": normalized.get("used_margin_basis"),
                "source": "gate_futures_account_balance",
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

    @staticmethod
    def _normalize_futures_balance(balance: Dict[str, Any], usdt: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Gate's native futures account response without CCXT inference.

        Gate's futures response is returned by CCXT in ``info`` as a one-item
        list.  The common CCXT balance parser intentionally maps ``used`` from
        ``freeze``/``locked``; those fields are absent on Gate futures and can
        therefore produce a nonsensical negative ``total - free`` value.  The
        native margin fields are the authority here.
        """

        raw = balance.get("info")
        account_info: Dict[str, Any] = {}
        if isinstance(raw, dict):
            account_info = raw
        elif isinstance(raw, list):
            candidates = [item for item in raw if isinstance(item, dict)]
            account_info = next(
                (item for item in candidates if str(item.get("currency") or "").upper() == "USDT"),
                candidates[0] if candidates else {},
            )

        def first_number(*values: Any) -> Optional[float]:
            for value in values:
                parsed = _optional_float(value)
                if parsed is not None:
                    return parsed
            return None

        total = first_number(account_info.get("total"), usdt.get("total"), balance.get("total"))
        free = first_number(
            account_info.get("available"),
            account_info.get("cross_available"),
            account_info.get("total_available_margin"),
            usdt.get("free"),
            balance.get("free"),
        )
        unrealized = first_number(
            account_info.get("unrealized_pnl"),
            account_info.get("unrealised_pnl"),
            usdt.get("unrealized_pnl"),
            usdt.get("unrealised_pnl"),
        )
        history = account_info.get("history") if isinstance(account_info.get("history"), dict) else {}
        realized = first_number(
            account_info.get("realized_pnl"),
            account_info.get("realised_pnl"),
            history.get("pnl"),
        )

        cross_pos_margin = first_number(account_info.get("cross_initial_margin"), account_info.get("position_initial_margin"))
        plain_pos_margin = first_number(account_info.get("position_margin"), account_info.get("init_margin"))
        position_margin = cross_pos_margin if (cross_pos_margin is not None and cross_pos_margin > 0) else (plain_pos_margin if plain_pos_margin is not None else cross_pos_margin)

        order_margin = first_number(account_info.get("cross_order_margin"), account_info.get("order_margin"))
        if order_margin is None:
            bid_margin = first_number(account_info.get("bid_order_margin"))
            ask_margin = first_number(account_info.get("ask_order_margin"))
            if bid_margin is not None or ask_margin is not None:
                order_margin = (bid_margin or 0.0) + (ask_margin or 0.0)
        margin_components = [value for value in (position_margin, order_margin) if value is not None]
        if margin_components:
            # Margin is a non-negative reserved quantity.  Do not let a
            # malformed provider sign turn used margin into a negative risk.
            used = sum(max(0.0, value) for value in margin_components)
            used_basis = "GATE_NATIVE_POSITION_PLUS_ORDER_MARGIN"
        else:
            native_used = first_number(account_info.get("used"), account_info.get("margin_used"), usdt.get("used"), balance.get("used"))
            used = native_used if native_used is not None and native_used >= 0 else None
            used_basis = "GATE_NATIVE_USED" if used is not None else "UNKNOWN_NATIVE_MARGIN"

        # In Gate single_currency cross-margin mode, the API returns total="0" while
        # the true usable capital is in available / cross_available. Derive the total
        # margin balance so risk engine does not treat a funded account as insolvent.
        margin_balance = ((free or 0.0) + (used or 0.0)) if free is not None else None
        if total is None or total <= 0 or (free is not None and total < free):
            if margin_balance is not None and margin_balance > 0:
                total = margin_balance

        explicit_equity = first_number(
            account_info.get("equity"),
            account_info.get("cross_margin_balance"),
            account_info.get("total_margin_balance"),
            account_info.get("unified_account_total_equity"),
        )
        if explicit_equity is not None and explicit_equity > 0:
            equity = explicit_equity
            equity_basis = "GATE_NATIVE_EQUITY"
        elif total is not None and total > 0:
            # Gate documents ``total`` as historical/account balance and
            # exposes current unrealised PNL separately.
            if unrealized is not None:
                equity = max(free or 0.0, total + unrealized)
            else:
                equity = max(free or 0.0, total)
            equity_basis = "GATE_TOTAL_PLUS_UNREALISED_PNL"
        elif free is not None:
            base = margin_balance or free
            equity = max(free, base + (unrealized or 0.0))
            equity_basis = "GATE_DERIVED_EQUITY"
        else:
            equity = total
            equity_basis = "GATE_TOTAL_ONLY" if total is not None else "UNKNOWN_EQUITY"

        return {
            "total": total,
            "free": free,
            "used": used,
            "equity": equity,
            "unrealized_pnl": unrealized,
            "realized_pnl": realized,
            "position_margin": position_margin,
            "order_margin": order_margin,
            "equity_basis": equity_basis,
            "available_margin_basis": "GATE_NATIVE_AVAILABLE" if free is not None else "UNKNOWN_AVAILABLE_MARGIN",
            "used_margin_basis": used_basis,
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
            raw_positions = _execute_with_retry(lambda: ex.fetch_positions())
            if not isinstance(raw_positions, list):
                raise ValueError("GATE_RESPONSE_SCHEMA_INVALID")
            try:
                markets = ex.load_markets()
            except Exception:
                markets = {}
            results = []
            degraded = False
            for position in raw_positions:
                if not isinstance(position, dict):
                    degraded = True
                    continue
                info = position.get("info") if isinstance(position.get("info"), dict) else {}
                raw_size = position.get("contracts")
                if raw_size is None:
                    raw_size = position.get("size", info.get("size"))
                size = _optional_float(raw_size)
                if size is None:
                    degraded = True
                    results.append({
                        "symbol": position.get("symbol"),
                        "side": str(position.get("side") or "UNKNOWN").upper(),
                        "contracts": None,
                        "position_status": "UNKNOWN_SIZE",
                    })
                    continue
                if abs(size) <= 0:
                    continue
                raw_side = str(position.get("side") or info.get("side") or "").strip().upper()
                if raw_side in {"BUY", "LONG"}:
                    normalized_side = "LONG"
                elif raw_side in {"SELL", "SHORT"}:
                    normalized_side = "SHORT"
                elif size < 0:
                    normalized_side = "SHORT"
                else:
                    normalized_side = "LONG"
                size = abs(size)
                market = markets.get(position.get("symbol")) if isinstance(markets, dict) else None
                if not isinstance(market, dict) and isinstance(markets, dict):
                    compact = _symbol_compact(position.get("symbol"))
                    market = next(
                        (
                            item for item in markets.values()
                            if isinstance(item, dict) and _symbol_compact(item.get("symbol") or item.get("id")) == compact
                        ),
                        {},
                    )
                contract_size = _optional_float(
                    position.get("contractSize")
                    or position.get("contract_size")
                    or info.get("contractSize")
                    or (market or {}).get("contractSize")
                )
                results.append({
                    "symbol": position.get("symbol"),
                    "side": normalized_side,
                    "contracts": size,
                    "entry_price": _optional_float(position.get("entryPrice", position.get("entry_price", info.get("entry_price")))),
                    "mark_price": _optional_float(position.get("markPrice", position.get("mark_price", info.get("mark_price")))),
                    "unrealized_pnl": _optional_float(position.get("unrealizedPnl", position.get("unrealized_pnl", position.get("unrealisedPnl", info.get("unrealised_pnl"))))),
                    "realized_pnl": _optional_float(position.get("realizedPnl", position.get("realized_pnl", position.get("realisedPnl", info.get("realised_pnl"))))),
                    "leverage": _optional_float(position.get("leverage", info.get("leverage"))),
                    "liquidation_price": _optional_float(position.get("liquidationPrice", position.get("liquidation_price", info.get("liq_price")))),
                    "initial_margin": _optional_float(position.get("initialMargin", position.get("initial_margin", info.get("initial_margin")))),
                    "contract_size": contract_size,
                    "position_id": position.get("id") or (
                        info.get("id")
                        if isinstance(info, dict)
                        else None
                    ),
                })
            self.last_positions_status = "DEGRADED" if degraded else "AVAILABLE"
            return results
        except Exception as exc:
            mapped = _map_gate_error(exc)
            logger.warning("Failed to fetch Gate %s positions (%s)", self.api_environment, mapped["code"])
            self.last_positions_status = "UNAVAILABLE"
            self.last_positions_error_code = mapped["code"]
            return []

    def get_open_orders(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve the account's currently pending Gate futures orders.

        An absent/unsupported endpoint is deliberately reported as degraded,
        not as an empty order list.  Empty and unavailable are different
        account facts for risk and reconciliation.
        """
        if not self.is_configured:
            self.last_open_orders_status = "NOT_CONFIGURED"
            self.last_open_orders_error_code = "GATE_CREDENTIALS_REQUIRED"
            return []
        self.last_open_orders_error_code = None
        try:
            ex = self._get_exchange()
            fetcher = getattr(ex, "fetch_open_orders", None)
            if not callable(fetcher):
                self.last_open_orders_status = "UNSUPPORTED"
                self.last_open_orders_error_code = "GATE_OPEN_ORDERS_UNSUPPORTED"
                return []
            target_symbol = self._exchange_symbol(ex, symbol) if symbol else None
            raw_orders = _execute_with_retry(lambda: fetcher(target_symbol, limit=max(1, min(int(limit), 100))))
            if not isinstance(raw_orders, list):
                raise ValueError("GATE_OPEN_ORDERS_RESPONSE_SCHEMA_INVALID")
            results: list[dict[str, Any]] = []
            degraded = False
            for order in raw_orders:
                if not isinstance(order, dict):
                    degraded = True
                    continue
                order_id = order.get("id")
                if not order_id:
                    degraded = True
                    continue
                results.append(
                    {
                        "order_id": str(order_id),
                        "client_order_id": order.get("clientOrderId"),
                        "symbol": str(order.get("symbol") or "").replace("/", "").replace(":USDT", "") or None,
                        "side": str(order.get("side") or "UNKNOWN").upper(),
                        "type": str(order.get("type") or "UNKNOWN").lower(),
                        "status": str(order.get("status") or "open").upper(),
                        "amount": _optional_float(order.get("amount")),
                        "filled": _optional_float(order.get("filled")),
                        "remaining": _optional_float(order.get("remaining")),
                        "price": _optional_float(order.get("price")),
                        "stop_price": _optional_float(order.get("triggerPrice") or order.get("stopPrice")),
                        "timestamp": order.get("timestamp"),
                        "datetime": order.get("datetime"),
                        "reduce_only": _optional_bool(
                            order.get("reduceOnly")
                            if order.get("reduceOnly") is not None
                            else ((order.get("info") or {}).get("reduce_only") if isinstance(order.get("info"), dict) else None)
                        ) is True,
                    }
                )
            self.last_open_orders_status = "DEGRADED" if degraded else "AVAILABLE"
            return results
        except Exception as exc:
            mapped = _map_gate_error(exc)
            logger.warning("Failed to fetch Gate %s open orders (%s)", self.api_environment, mapped["code"])
            self.last_open_orders_status = "UNAVAILABLE"
            self.last_open_orders_error_code = mapped["code"]
            return []

    def get_market_metadata(self, symbol: str) -> Dict[str, Any]:
        """Return explicit Gate contract metadata for remote sizing."""
        ex = self._get_exchange()
        markets = ex.load_markets()
        if not isinstance(markets, dict):
            raise ValueError("GATE_MARKETS_RESPONSE_SCHEMA_INVALID")
        exchange_symbol = self._exchange_symbol(ex, symbol)
        market = markets.get(exchange_symbol)
        if not isinstance(market, dict):
            for value in markets.values():
                if isinstance(value, dict) and str(value.get("symbol") or "") == exchange_symbol:
                    market = value
                    break
        if not isinstance(market, dict):
            raise ValueError("GATE_MARKET_NOT_FOUND")
        if not (market.get("swap") and market.get("linear") and str(market.get("settle") or "").upper() == "USDT"):
            raise ValueError("GATE_MARKET_NOT_LINEAR_USDT_PERPETUAL")
        precision = dict(market.get("precision") or {})
        limits = dict(market.get("limits") or {})
        amount_limits = dict(limits.get("amount") or {})
        raw_info = market.get("info") if isinstance(market.get("info"), dict) else {}
        contract_size = _optional_float(market.get("contractSize"))
        # Gate futures amounts are contract counts.  In CCXT's Gate market
        # map ``precision.amount`` is a tick size (BTC/USDT:USDT is 1), not a
        # number of decimal places.  Prefer an explicit limit step, then the
        # native Gate order-size bounds, and only then the CCXT precision.
        amount_step = _optional_float(
            amount_limits.get("step")
            or raw_info.get("order_size_min")
            or precision.get("amount")
        )
        amount_min = _optional_float(amount_limits.get("min") or raw_info.get("order_size_min"))
        amount_max = _optional_float(amount_limits.get("max") or raw_info.get("order_size_max"))
        price_tick = _optional_float(precision.get("price"))
        if contract_size is None or amount_step is None or amount_min is None or amount_max is None or price_tick is None:
            raise ValueError("GATE_MARKET_METADATA_INCOMPLETE")
        if amount_step <= 0 or amount_min <= 0 or amount_max < amount_min:
            raise ValueError("GATE_MARKET_METADATA_INVALID")
        return {
            "symbol": str(symbol).strip().upper(),
            "native_symbol": str(market.get("id") or exchange_symbol),
            "ccxt_symbol": exchange_symbol,
            "market_type": "perpetual",
            "contract_type": "perpetual",
            "contractSize": contract_size,
            "precision": {"amount": amount_step, "price": price_tick},
            "limits": {"amount": {"step": amount_step, "min": amount_min, "max": amount_max}},
            "amount_unit": "CONTRACTS",
            "amount_semantics": "Gate futures order amount is an integer/step contract count, not base-asset quantity.",
            "taker": _optional_float(market.get("taker")) if market.get("taker") is not None else None,
            "maker": _optional_float(market.get("maker")) if market.get("maker") is not None else None,
            "active": bool(market.get("active", True)),
            "source": "gate_ccxt_private_market_metadata",
            "raw": {key: value for key, value in market.items() if key not in {"info"}},
        }

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Read one current Gate ticker for the explicit TestNet order test."""

        if not self.is_configured:
            return {
                "status": "NOT_CONFIGURED",
                "error_code": "GATE_CREDENTIALS_REQUIRED",
                "symbol": symbol,
                "last": None,
                "bid": None,
                "ask": None,
                "observed_at": None,
            }
        try:
            ex = self._get_exchange()
            fetcher = getattr(ex, "fetch_ticker", None)
            if not callable(fetcher):
                return {"status": "UNSUPPORTED", "error_code": "GATE_TICKER_UNSUPPORTED", "symbol": symbol, "last": None}
            exchange_symbol = self._exchange_symbol(ex, symbol)
            ticker = fetcher(exchange_symbol)
            if not isinstance(ticker, dict):
                raise ValueError("GATE_TICKER_RESPONSE_SCHEMA_INVALID")
            last = _optional_float(ticker.get("last") or ticker.get("close"))
            bid = _optional_float(ticker.get("bid"))
            ask = _optional_float(ticker.get("ask"))
            if last is None or last <= 0:
                raise ValueError("GATE_TICKER_LAST_MISSING")
            timestamp = ticker.get("timestamp")
            observed_at = (
                datetime.fromtimestamp(float(timestamp) / 1000.0, timezone.utc).isoformat()
                if timestamp is not None
                else datetime.now(timezone.utc).isoformat()
            )
            return {
                "status": "AVAILABLE",
                "symbol": str(symbol).strip().upper(),
                "native_symbol": exchange_symbol,
                "last": last,
                "bid": bid,
                "ask": ask,
                "observed_at": observed_at,
                "source": "gate_testnet_private_exchange_ticker",
            }
        except Exception as exc:
            mapped = _map_gate_error(exc)
            return {
                "status": "UNAVAILABLE",
                "error_code": mapped["code"],
                "message_zh": mapped["message_zh"],
                "symbol": symbol,
                "last": None,
            }

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 64) -> list[list[Any]]:
        """Read OHLCV only for explicit E2E ATR derivation; no local fallback."""

        if not self.is_configured:
            raise ValueError("GATE_CREDENTIALS_REQUIRED")
        ex = self._get_exchange()
        fetcher = getattr(ex, "fetch_ohlcv", None)
        if not callable(fetcher):
            raise ValueError("GATE_OHLCV_UNSUPPORTED")
        result = fetcher(self._exchange_symbol(ex, symbol), timeframe, None, max(2, min(int(limit), 200)))
        if not isinstance(result, list) or len(result) < 2:
            raise ValueError("GATE_OHLCV_INSUFFICIENT")
        return result

    def connection_test(self) -> Dict[str, Any]:
        """Perform bounded, read-only account and endpoint verification."""
        validation = self.validate_credentials()
        if validation.get("valid") is not True:
            return {
                **validation,
                "read_only": True,
                "orders_sent": 0,
                "model_called": False,
                "authorization_created": False,
                "positions": [],
                "pending_orders": [],
                "markets": {"status": "NOT_RUN"},
            }
        balance = self.get_account_balance()
        positions = self.get_positions()
        pending_orders = self.get_open_orders(limit=100)
        markets_status: dict[str, Any] = {"status": "UNKNOWN", "count": None}
        try:
            markets = self._get_exchange().load_markets()
            if isinstance(markets, dict):
                markets_status = {
                    "status": "AVAILABLE",
                    "count": sum(1 for item in markets.values() if isinstance(item, dict) and item.get("swap") and item.get("linear")),
                }
        except Exception as exc:
            mapped = _map_gate_error(exc)
            markets_status = {"status": "UNAVAILABLE", "error_code": mapped["code"], "message_zh": mapped["message_zh"]}
        component_statuses = {
            "balance": balance.get("data_status"),
            "positions": self.last_positions_status,
            "pending_orders": self.last_open_orders_status,
            "markets": markets_status.get("status"),
        }
        all_available = all(value == "AVAILABLE" for value in component_statuses.values())
        return {
            "valid": True,
            "status": "VERIFIED_READ_ONLY" if all_available else "VERIFIED_READ_ONLY_DEGRADED",
            "code": "GATE_CONNECTION_VERIFIED" if all_available else "GATE_CONNECTION_DEGRADED",
            "message_zh": "Gate 只读连接、账户、持仓和挂单查询成功。" if all_available else "Gate 凭证有效，但部分只读账户事实未能完整读取。",
            "api_environment": self.api_environment,
            "endpoint": self.api_base_url,
            "read_only": True,
            "balance": balance,
            "positions": positions,
            "pending_orders": pending_orders,
            "markets": markets_status,
            "component_statuses": component_statuses,
            "permissions": {"orders": "UNKNOWN_NOT_PROBED", "reason": "connection_test_never_submits_or_modifies_orders"},
            "orders_sent": 0,
            "model_called": False,
            "authorization_created": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_account_truth(self, *, include_trades: bool = False) -> Dict[str, Any]:
        """Fetch the remote account facts used by risk and UI projections."""
        balance = self.get_account_balance()
        positions = self.get_positions()
        pending_orders = self.get_open_orders(limit=100)
        fills = self.get_trades(limit=100) if include_trades else []
        values = [item.get("unrealized_pnl") for item in positions]
        unrealized = sum((float(value) for value in values if _optional_float(value) is not None), 0.0) if values and all(_optional_float(value) is not None for value in values) else None
        statuses = [str(balance.get("data_status") or "UNKNOWN"), self.last_positions_status, self.last_open_orders_status]
        if include_trades:
            statuses.append(self.last_trades_status)
        status = "AVAILABLE" if all(value == "AVAILABLE" for value in statuses) else ("DEGRADED" if any(value in {"AVAILABLE", "DEGRADED"} for value in statuses) else "UNAVAILABLE")
        return {
            "status": status,
            "source": f"Gate.io v4 {self.api_environment} Private API",
            "api_environment": self.api_environment,
            "endpoint": self.api_base_url,
            "observed_at": balance.get("observed_at") or datetime.now(timezone.utc).isoformat(),
            "equity": balance.get("equity"),
            "available_margin": balance.get("free"),
            "used_margin": balance.get("used"),
            "unrealized_pnl": balance.get("unrealized_pnl") if balance.get("unrealized_pnl") is not None else unrealized,
            "realized_pnl": balance.get("realized_pnl"),
            "balance": balance,
            "positions": positions,
            "pending_orders": pending_orders,
            "fills": fills,
            "positions_status": self.last_positions_status,
            "pending_orders_status": self.last_open_orders_status,
            "fills_status": self.last_trades_status if include_trades else "NOT_REQUESTED",
            "error_code": next((getattr(self, attr, None) for attr in ("last_positions_error_code", "last_open_orders_error_code", "last_trades_error_code") if getattr(self, attr, None)), None),
            "remote_truth": True,
        }

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
            
            raw_trades = _execute_with_retry(lambda: ex.fetch_my_trades(target_symbol, limit=max(1, min(limit, 100))))
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
        if leverage is None:
            return {"acknowledged": False, "error": "LEVERAGE_REQUIRED: explicit leverage in [1, 100] is required", "symbol": symbol, "leverage": None}
        try:
            leverage_int = int(round(float(leverage)))
        except (TypeError, ValueError):
            return {"acknowledged": False, "error": "LEVERAGE_REQUIRED: explicit leverage in [1, 100] is required", "symbol": symbol, "leverage": None}
        if leverage_int < 1 or leverage_int > 100:
            return {"acknowledged": False, "error": "LEVERAGE_REQUIRED: explicit leverage in [1, 100] is required", "symbol": symbol, "leverage": None}
        leverage = leverage_int
        if not self.live_trading_enabled:
            return {"acknowledged": True, "dry_run": True, "symbol": symbol, "leverage": leverage}
        try:
            ex = self._get_exchange()
            exchange_symbol = self._exchange_symbol(ex, symbol)
            margin_mode, positions_observed = self._remote_margin_mode(ex, exchange_symbol)
            if not positions_observed:
                return {
                    "acknowledged": False,
                    "error_code": "GATE_MARGIN_MODE_UNAVAILABLE",
                    "message_zh": "未能读取 Gate 远端保证金模式；为避免切换已有仓位的逐仓/全仓模式，未提交杠杆变更。",
                    "symbol": symbol,
                    "leverage": leverage,
                }
            # No existing position means there is no remote mode to preserve.
            # Choose the explicit Gate isolated default rather than allowing
            # CCXT to make an undocumented implicit choice.
            margin_mode_source = "REMOTE_POSITION" if margin_mode is not None else "NO_EXISTING_POSITION_DEFAULT"
            margin_mode = margin_mode or "isolated"
            res = ex.set_leverage(leverage, exchange_symbol, {"marginMode": margin_mode})
            return {
                "acknowledged": True,
                "dry_run": False,
                "symbol": symbol,
                "leverage": leverage,
                "margin_mode": margin_mode,
                "margin_mode_source": margin_mode_source,
                "result": res,
            }
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
        for either environment only after its account-scoped credential has
        been selected; a disabled adapter is validation-only and never
        fabricates a fill.
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
                gate_text = client_order_id if str(client_order_id).startswith("t-") else f"t-{client_order_id}"
                # Gate v4 limits the client text field to 28 characters.
                # Truncate only this transport correlation value; the full
                # id remains in the local receipt/audit record.
                params["text"] = str(gate_text)[:28]
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
            raw_info = order.get("info") if isinstance(order.get("info"), dict) else {}
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
                # Preserve only decision-relevant remote receipt facts.  A
                # transport acknowledgement is never a fill confirmation,
                # but retaining these fields makes an immediate Gate cancel
                # or reject observable instead of silently showing ACK.
                "remote_status": raw_status or raw_info.get("status"),
                "remote_finish_as": raw_info.get("finish_as"),
                "remote_label": raw_info.get("label") or raw_info.get("code"),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "fills": fills,
                "created_at": now_iso,
                "protection_status": "NOT_REQUESTED",
                "protection_orders": [],
                "stop_loss": stop_loss,
                "take_profit": take_profit,
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
                        response["protection_verified"] = True
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

    def place_protection_orders(
        self,
        symbol: str,
        *,
        side: str,
        amount: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        client_order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Place native conditional SL/TP orders on Gate for an observed fill."""
        if not self.live_trading_enabled:
            return []
        ex = self._get_exchange()
        exchange_symbol = self._exchange_symbol(ex, symbol)
        ccxt_side = "buy" if str(side).lower() in ("buy", "long") else "sell"
        return self._place_protection_orders(
            ex,
            exchange_symbol,
            side=ccxt_side,
            amount=amount,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_order_id=client_order_id,
        )

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
                "amount": amount,
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
