"""Explicit Gate TestNet order-to-cleanup acceptance flow.

This module is intentionally separate from the AI-led coordinator and the
normal authorization gateway.  It is a user-triggered transport check for a
TestNet account, not a paper simulator and not a production trading engine.
It sends a real TestNet order only when the caller explicitly confirms the
test, then reconciles the remote order/position/protection and performs a
reduce-only cleanup when requested.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
import hashlib
import json
import math
import time
from typing import Any, Callable, Dict, Iterable

from .account_aliases import canonical_account_id


class GateE2EError(Exception):
    def __init__(self, code: str, message_zh: str, status_code: int = 422) -> None:
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.status_code = status_code


def _finite(value: Any, *, positive: bool = False) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or (positive and parsed <= 0):
        return None
    return parsed


def _safe_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(v) for v in value]
    return value if value is None or isinstance(value, (str, int, bool)) else str(value)


def _text(value: Any) -> str:
    return json.dumps(_safe_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper().replace("/", "").replace(":USDT", "").replace("_", "").replace("-", "")


def _align_price(value: Decimal, tick: Decimal, *, rounding: str = ROUND_DOWN) -> Decimal:
    if tick <= 0:
        raise GateE2EError("GATE_MARKET_METADATA_INCOMPLETE", "Gate 合约价格最小变动单位不可用。")
    return (value / tick).quantize(Decimal("1"), rounding=rounding) * tick


def _derive_atr(rows: Iterable[Any], period: int = 14) -> Decimal:
    parsed: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for item in rows:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        timestamp = _finite(item[0])
        opened = _finite(item[1], positive=True)
        high = _finite(item[2], positive=True)
        low = _finite(item[3], positive=True)
        close = _finite(item[4], positive=True)
        if timestamp is None or opened is None or high is None or low is None or close is None:
            continue
        parsed.append((opened, high, low, close))
    if len(parsed) < period + 1:
        raise GateE2EError("ATR_UNAVAILABLE", "Gate TestNet 无足够已闭合 K 线计算 ATR。")
    ranges: list[Decimal] = []
    previous_close = parsed[0][3]
    for _opened, high, low, close in parsed[1:]:
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    if len(ranges) < period:
        raise GateE2EError("ATR_UNAVAILABLE", "Gate TestNet 无足够已闭合 K 线计算 ATR。")
    return sum(ranges[-period:], Decimal("0")) / Decimal(period)


class GateTestnetE2EService:
    """Run one idempotent, explicitly confirmed TestNet acceptance test."""

    def __init__(self, store: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ensure_tables()

    def ensure_tables(self) -> None:
        with self.store._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS gate_testnet_e2e_runs (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(account_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_gate_testnet_e2e_runs_scope
                    ON gate_testnet_e2e_runs(account_id, started_at DESC);
                """
            )

    def _existing(self, account_id: str, idempotency_key: str, request_hash: str) -> Dict[str, Any] | None:
        with self.store._connect() as db:
            row = db.execute(
                "SELECT * FROM gate_testnet_e2e_runs WHERE account_id=? AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise GateE2EError("IDEMPOTENCY_CONFLICT", "同一 TestNet 验收幂等键对应了不同请求参数。", 409)
        try:
            result = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {}
        return result if isinstance(result, dict) else {}

    def _save(self, run_id: str, account_id: str, idem: str, request_hash: str, status: str, stage: str, started: str, completed: str | None, result: Dict[str, Any]) -> None:
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO gate_testnet_e2e_runs(
                    run_id, account_id, environment, idempotency_key,
                    request_hash, status, current_stage, started_at, completed_at,
                    result_json
                ) VALUES (?, ?, 'testnet', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, idempotency_key) DO UPDATE SET
                    status=excluded.status, current_stage=excluded.current_stage,
                    completed_at=excluded.completed_at, result_json=excluded.result_json""",
                (run_id, account_id, idem, request_hash, status, stage, started, completed, _text(result)),
            )

    @staticmethod
    def _stage(name: str, status: str, *, reason_code: str | None = None, message_zh: str | None = None, evidence: Any = None, started_at: str | None = None, completed_at: str | None = None) -> Dict[str, Any]:
        start = started_at or datetime.now(timezone.utc).isoformat()
        end = completed_at or (start if status in {"BLOCKED", "FAILED", "COMPLETED", "SKIPPED"} else None)
        duration = None
        if end:
            try:
                duration = max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000.0)
            except (TypeError, ValueError):
                duration = None
        return {
            "stage": name,
            "status": status,
            "started_at": start,
            "completed_at": end,
            "duration_ms": duration,
            "reason_code": reason_code,
            "message_zh": message_zh,
            "evidence": _safe_json(evidence or {}),
        }

    @staticmethod
    def _remote_position(truth: Dict[str, Any], symbol: str, side: str) -> Dict[str, Any] | None:
        for position in truth.get("positions") or []:
            if not isinstance(position, dict):
                continue
            if _symbol(position.get("symbol")) != _symbol(symbol):
                continue
            position_side = str(position.get("side") or "").upper()
            if position_side in {side, "BUY" if side == "LONG" else "SELL"}:
                contracts = _finite(position.get("contracts", position.get("size")), positive=True)
                if contracts is not None:
                    return {**position, "contracts": str(contracts)}
        return None

    def run(self, request: Dict[str, Any], trader: Any) -> Dict[str, Any]:
        from .gate_accounts import GATE_TESTNET_ACCOUNT_TYPE, get_gate_account_profile

        account_id = canonical_account_id(self.store, str(request.get("account_id") or ""))
        if not account_id:
            raise GateE2EError("ACCOUNT_REQUIRED", "TestNet 验收必须明确指定账户。")
        profile = get_gate_account_profile(self.store, account_id)
        if profile.get("account_type") != GATE_TESTNET_ACCOUNT_TYPE or profile.get("execution_mode") != "TESTNET":
            raise GateE2EError("LIVE_DISABLED_BY_RELEASE_POLICY", "该验收脚本只允许 Gate 官方 TestNet，Live 永远锁定。", 403)
        if request.get("confirm_testnet") is not True:
            raise GateE2EError("TESTNET_CONFIRMATION_REQUIRED", "请明确确认这是会向 Gate TestNet 发送真实测试订单。")

        symbol = _symbol(request.get("symbol"))
        side = str(request.get("side") or "").upper()
        if not symbol or side not in {"LONG", "SHORT"}:
            raise GateE2EError("TESTNET_ORDER_PARAMETERS_INVALID", "TestNet 验收需要合法交易标的和 LONG/SHORT 方向。")
        stop_type = str(request.get("stop_type") or "PRICE").upper()
        take_type = str(request.get("take_profit_type") or "PRICE").upper()
        if stop_type not in {"PRICE", "PERCENT", "ATR"} or take_type not in {"PRICE", "PERCENT", "ATR"}:
            raise GateE2EError("PROTECTION_TYPE_INVALID", "止损和止盈类型必须是 PRICE、PERCENT 或 ATR。")
        idem = str(request.get("idempotency_key") or "").strip()[:160]
        if not idem:
            raise GateE2EError("IDEMPOTENCY_KEY_REQUIRED", "TestNet 验收必须提供幂等键，避免重复下单。")
        request_for_hash = {key: request.get(key) for key in ("account_id", "symbol", "side", "stop_type", "stop_value", "take_profit_type", "take_profit_value", "leverage", "cleanup")}
        request_hash = hashlib.sha256(_text(request_for_hash).encode("utf-8")).hexdigest()
        previous = self._existing(account_id, idem, request_hash)
        if previous is not None:
            previous["idempotent_replay"] = True
            return previous
        if trader is None or not bool(getattr(trader, "testnet", False)) or not bool(getattr(trader, "live_trading_enabled", False)):
            raise GateE2EError("GATE_TESTNET_CREDENTIALS_NOT_CONFIGURED", "Gate TestNet 适配器或凭证未配置，未发送订单。")

        started = datetime.now(timezone.utc).isoformat()
        run_id = f"gate_e2e_{hashlib.sha256((account_id + idem).encode()).hexdigest()[:24]}"
        result: Dict[str, Any] = {
            "run_id": run_id,
            "account_id": account_id,
            "environment": "TESTNET",
            "venue": "gate",
            "idempotency_key": idem,
            "status": "RUNNING",
            "orders_sent": 0,
            "model_called": False,
            "authorization_called": False,
            "local_fill_created": False,
            "stages": [],
            "started_at": started,
        }
        self._save(run_id, account_id, idem, request_hash, "RUNNING", "METADATA", started, None, result)

        try:
            metadata = trader.get_market_metadata(symbol)
            market = metadata if isinstance(metadata, dict) else {}
            precision = market.get("precision") or {}
            limits = (market.get("limits") or {}).get("amount") or {}
            tick = _finite(precision.get("price"), positive=True)
            step = _finite(precision.get("amount") or limits.get("step"), positive=True)
            minimum = _finite(limits.get("min"), positive=True)
            maximum = _finite(limits.get("max"), positive=True)
            contract_size = _finite(market.get("contractSize"), positive=True)
            fee_rate = _finite(market.get("taker"), positive=False)
            if fee_rate is None or fee_rate < 0:
                fee_rate = None
            if not tick or not step or not minimum or not maximum or not contract_size or fee_rate is None:
                raise GateE2EError("GATE_MARKET_METADATA_INCOMPLETE", "Gate TestNet 未返回完整的合约单位、数量步长、上下限和费率。")
            amount = (minimum / step).quantize(Decimal("1"), rounding=ROUND_UP) * step
            if amount < minimum or amount > maximum:
                raise GateE2EError("GATE_TESTNET_MINIMUM_SIZE_INVALID", "Gate TestNet 最小可下单数量无法按步长表示。")
            result["metadata"] = {key: _safe_json(value) for key, value in market.items() if key != "raw"}
            result["amount"] = str(amount)
            result["contract_size"] = str(contract_size)
            result["stages"].append(self._stage("METADATA", "COMPLETED", message_zh="已从 Gate TestNet 读取合约最小数量、精度和费率。", evidence={"source": metadata.get("source"), "amount": str(amount), "contract_size": str(contract_size)}))

            ticker = trader.get_ticker(symbol)
            if not isinstance(ticker, dict) or ticker.get("status") != "AVAILABLE":
                raise GateE2EError(str((ticker or {}).get("error_code") or "GATE_TICKER_UNAVAILABLE"), "Gate TestNet 当前行情不可用，未发送订单。")
            last = _finite(ticker.get("last"), positive=True)
            if not last:
                raise GateE2EError("GATE_TICKER_UNAVAILABLE", "Gate TestNet 当前价格不可用，未发送订单。")

            atr_cache: Decimal | None = None
            def level(kind: str, raw_value: Any, *, is_stop: bool) -> Decimal:
                nonlocal atr_cache
                value = _finite(raw_value, positive=True)
                if value is None:
                    raise GateE2EError("PROTECTION_VALUE_REQUIRED", "TestNet 验收必须提供正的止损/止盈参数。")
                is_up = (side == "LONG") != is_stop
                if kind == "PRICE":
                    raw = value
                elif kind == "PERCENT":
                    if value > Decimal("50"):
                        raise GateE2EError("PROTECTION_PERCENT_INVALID", "止损/止盈百分比必须不大于 50%。")
                    factor = value / Decimal("100")
                    raw = last * (Decimal("1") + (factor if is_up else -factor))
                else:
                    if atr_cache is None:
                        atr_cache = _derive_atr(trader.get_ohlcv(symbol, "15m", 64))
                    if atr_cache <= 0:
                        raise GateE2EError("ATR_UNAVAILABLE", "Gate TestNet ATR 不为正，未发送订单。")
                    raw = last + (atr_cache * value if is_up else -atr_cache * value)
                aligned = _align_price(raw, tick, rounding=ROUND_UP if is_up else ROUND_DOWN)
                if aligned <= 0:
                    raise GateE2EError("PROTECTION_LEVEL_INVALID", "止损/止盈按 Gate 价格精度对齐后不为正。")
                return aligned

            stop = level(stop_type, request.get("stop_value"), is_stop=True)
            take_profit = level(take_type, request.get("take_profit_value"), is_stop=False)
            if (side == "LONG" and not (stop < last < take_profit)) or (side == "SHORT" and not (take_profit < last < stop)):
                raise GateE2EError("PROTECTION_DIRECTION_INVALID", "Gate TestNet 止损/止盈方向与订单方向不一致。")
            result.update({"ticker": _safe_json(ticker), "entry_reference": str(last), "stop_price": str(stop), "take_profit": str(take_profit)})
            result["stages"].append(self._stage("PROTECTION_DERIVED", "COMPLETED", message_zh="止损/止盈已按 Gate TestNet 价格精度和方向约束推导。", evidence={"stop": str(stop), "take_profit": str(take_profit), "source": "gate_testnet_market_metadata_and_ticker"}))

            leverage_value = _finite(request.get("leverage") or 1, positive=True)
            if leverage_value is None or leverage_value != leverage_value.to_integral_value():
                raise GateE2EError("LEVERAGE_INVALID", "TestNet 验收杠杆必须是 1 到 100 的整数。")
            leverage = int(leverage_value)
            if leverage < 1 or leverage > 100:
                raise GateE2EError("LEVERAGE_INVALID", "TestNet 验收杠杆必须在 1 到 100 之间。")
            lev_result = trader.set_leverage(symbol, leverage)
            if not isinstance(lev_result, dict) or lev_result.get("acknowledged") is not True:
                raise GateE2EError(str((lev_result or {}).get("error_code") or "GATE_LEVERAGE_REJECTED"), "Gate TestNet 杠杆设置未确认，未发送订单。")
            result["stages"].append(self._stage("LEVERAGE", "COMPLETED", message_zh="Gate TestNet 杠杆设置已确认。", evidence={"leverage": leverage}))

            client_order_id = f"t-e2e-{hashlib.sha256((account_id + idem).encode()).hexdigest()[:20]}"
            entry_side = side
            receipt = trader.place_order(symbol=symbol, side=entry_side, amount=float(amount), order_type="market", stop_loss=float(stop), take_profit=float(take_profit), leverage=leverage, reduce_only=False, client_order_id=client_order_id)
            if not isinstance(receipt, dict):
                raise GateE2EError("GATE_ORDER_RESPONSE_SCHEMA_INVALID", "Gate TestNet 下单响应格式异常，必须对账。")
            result["orders_sent"] = 1
            result["entry_order"] = _safe_json(receipt)
            result["stages"].append(self._stage("ORDER_SUBMITTED", "COMPLETED" if str(receipt.get("status") or "").upper() in {"ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED"} else "FAILED", reason_code=receipt.get("error_code"), message_zh="TestNet 订单已发送，正在读取远端订单状态。", evidence={"order_id": receipt.get("order_id"), "status": receipt.get("status"), "dry_run": receipt.get("dry_run", False)}))
            if receipt.get("dry_run") is True:
                raise GateE2EError("DRY_RUN_NOT_ALLOWED_FOR_E2E", "独立 TestNet 验收不能使用 dry-run。")
            order_id = receipt.get("order_id") or receipt.get("id")
            reconciled = receipt
            if str(receipt.get("status") or "").upper() in {"ACKNOWLEDGED", "UNKNOWN", "PARTIALLY_FILLED"} and order_id and callable(getattr(trader, "reconcile_order", None)):
                for attempt in range(5):
                    reconciled = trader.reconcile_order(str(order_id), symbol)
                    if str(reconciled.get("status") or "").upper() in {"FILLED", "PARTIALLY_FILLED", "CANCELED", "REJECTED"}:
                        break
                    if attempt < 4:
                        time.sleep(0.2)
            if str(reconciled.get("status") or "").upper() not in {"FILLED", "PARTIALLY_FILLED"} or _finite(reconciled.get("filled", reconciled.get("filled_quantity")), positive=True) is None:
                raise GateE2EError("REMOTE_ORDER_NOT_FILLED", "Gate TestNet 未确认入场成交；保持远端对账状态，未伪造本地成交。")
            result["entry_reconciliation"] = _safe_json(reconciled)
            result["stages"].append(self._stage("FILL_RECONCILED", "COMPLETED", message_zh="Gate TestNet 已返回具体成交数量，成交事实已确认。", evidence={"order_id": order_id, "status": reconciled.get("status"), "filled": reconciled.get("filled", reconciled.get("filled_quantity"))}))

            truth = trader.get_account_truth(include_trades=True)
            if not isinstance(truth, dict) or str(truth.get("status") or "").upper() != "AVAILABLE":
                raise GateE2EError("REMOTE_ACCOUNT_TRUTH_UNAVAILABLE", "入场后无法取得完整 Gate TestNet 账户/持仓事实，未盲目清理。")
            result["post_entry_truth"] = {key: _safe_json(truth.get(key)) for key in ("status", "observed_at", "equity", "available_margin", "positions", "pending_orders", "fills")}
            position = self._remote_position(truth, symbol, side)
            if position is None:
                raise GateE2EError("REMOTE_POSITION_NOT_RECONCILED", "Gate TestNet 成交已返回，但远端持仓尚未对账确认。")
            result["remote_position"] = _safe_json(position)
            result["stages"].append(self._stage("POSITION_RECONCILED", "COMPLETED", message_zh="Gate TestNet 远端持仓已确认。", evidence={"symbol": symbol, "side": side, "contracts": position.get("contracts")}))
            protection_status = str(receipt.get("protection_status") or "").upper()
            if protection_status != "PROTECTED":
                raise GateE2EError("REMOTE_PROTECTION_NOT_CONFIRMED", "Gate TestNet 入场成交已存在，但原生止损/止盈未确认。")
            result["stages"].append(self._stage("PROTECTION_RECONCILED", "COMPLETED", message_zh="Gate TestNet 原生 reduce-only 止损/止盈已确认。", evidence={"protection_status": protection_status, "protection_orders": receipt.get("protection_orders")}))

            cleanup = bool(request.get("cleanup", True))
            if cleanup:
                cleanup_side = "SHORT" if side == "LONG" else "LONG"
                cleanup_id = f"{client_order_id}-close"
                cleanup_receipt = trader.place_order(symbol=symbol, side=cleanup_side, amount=float(_finite(position.get("contracts"), positive=True) or amount), order_type="market", leverage=leverage, reduce_only=True, client_order_id=cleanup_id)
                result["orders_sent"] = int(result.get("orders_sent", 0)) + 1
                result["cleanup_order"] = _safe_json(cleanup_receipt)
                cleanup_status = str((cleanup_receipt or {}).get("status") or "").upper()
                if cleanup_status not in {"FILLED", "PARTIALLY_FILLED", "ACKNOWLEDGED"}:
                    raise GateE2EError("CLEANUP_ORDER_FAILED", "Gate TestNet reduce-only 清理订单未被确认。")
                post_cleanup = trader.get_account_truth(include_trades=True)
                result["post_cleanup_truth"] = {key: _safe_json(post_cleanup.get(key)) for key in ("status", "observed_at", "equity", "available_margin", "positions", "pending_orders", "fills")}
                remaining = self._remote_position(post_cleanup, symbol, side) if isinstance(post_cleanup, dict) else None
                if remaining is not None:
                    raise GateE2EError("CLEANUP_NOT_RECONCILED", "Gate TestNet 清理后仍有远端持仓，必须人工继续对账。")
                for leg in receipt.get("protection_orders") or []:
                    if isinstance(leg, dict) and leg.get("order_id") and callable(getattr(trader, "cancel_order", None)):
                        trader.cancel_order(str(leg["order_id"]), symbol)
                result["stages"].append(self._stage("CLEANUP", "COMPLETED", message_zh="Gate TestNet reduce-only 清理已完成并复核无剩余持仓。", evidence={"cleanup_status": cleanup_status}))
            else:
                result["stages"].append(self._stage("CLEANUP", "SKIPPED", message_zh="调用方选择保留 TestNet 持仓；请勿把本次验收当作已清仓。", evidence={"cleanup": False}))

            result["status"] = "COMPLETED"
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._save(run_id, account_id, idem, request_hash, "COMPLETED", "FINAL_RECONCILIATION", started, result["completed_at"], result)
            return result
        except GateE2EError as exc:
            result["status"] = "BLOCKED" if result.get("orders_sent", 0) == 0 else "RECONCILIATION_REQUIRED"
            result["error_code"] = exc.code
            result["message_zh"] = exc.message_zh
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            result["stages"].append(self._stage("FINAL_RECONCILIATION", "BLOCKED", reason_code=exc.code, message_zh=exc.message_zh))
            self._save(run_id, account_id, idem, request_hash, result["status"], "FINAL_RECONCILIATION", started, result["completed_at"], result)
            return result
        except Exception as exc:
            result["status"] = "RECONCILIATION_REQUIRED" if result.get("orders_sent", 0) else "BLOCKED"
            result["error_code"] = "GATE_E2E_UNEXPECTED_ERROR"
            result["message_zh"] = f"TestNet 验收异常：{type(exc).__name__}，请先对账再重试。"
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            result["stages"].append(self._stage("FINAL_RECONCILIATION", "FAILED", reason_code="GATE_E2E_UNEXPECTED_ERROR", message_zh=result["message_zh"]))
            self._save(run_id, account_id, idem, request_hash, result["status"], "FINAL_RECONCILIATION", started, result["completed_at"], result)
            return result


__all__ = ["GateE2EError", "GateTestnetE2EService"]
