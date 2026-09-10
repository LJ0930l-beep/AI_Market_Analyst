"""Account-scoped Gate.io profiles and credential routing.

The old Gate integration had one global credential slot.  That made a paper
configuration overwrite the live configuration (or vice versa) while the
rest of the product already treated ``account_id`` as the execution scope.
This module is the single profile boundary for the two supported Gate
accounts:

* ``gate_testnet``: canonical Gate official TestNet account.  It is remote,
  account-scoped, and never backed by a local fill simulator.
* ``gate_paper``: input-only compatibility alias retained for old clients.
* ``gate_live``: LIVE account metadata and encrypted credentials, still
  subject to the existing release-policy lock in ``ExecutionGateway``.

Provisioning is explicit and idempotent.  It never validates credentials or
calls a private exchange endpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Dict, Iterable, Optional, Tuple

from core.security.credentials import CredentialVault
from core.trading.ledger import AccountLedger
from core.trading.account_aliases import (
    GATE_TESTNET_ACCOUNT_ID,
    LEGACY_GATE_PAPER_ACCOUNT_ID,
    canonical_account_id,
)


GATE_VENUE = "gate"
GATE_MARKET_TYPE = "perpetual"
GATE_SETTLE_CURRENCY = "USDT"
# Kept as a Python compatibility constant for existing callers.  Its value is
# canonical so new code importing the historical name cannot create a second
# account or accidentally enter the local PAPER matcher.
GATE_PAPER_ACCOUNT_ID = GATE_TESTNET_ACCOUNT_ID
GATE_LIVE_ACCOUNT_ID = "gate_live"

# Gate API v4 futures-compatible base URLs.  They are persisted as explicit
# account metadata so an adapter cannot silently route a TestNet account to the
# live host.  ``gate_paper`` keeps its legacy PAPER row label, but its
# authoritative execution environment is the remote Gate official TestNet.
# ``gate_paper`` is input-only and resolves to ``gate_testnet``; it is not a
# second local PAPER account.
GATE_PAPER_API_BASE_URL = "https://api-testnet.gateapi.io/api/v4"
GATE_LIVE_API_BASE_URL = "https://api.gateio.ws/api/v4"
GATE_PROFILE_VERSION = "gate-account-v1"
LIVE_RELEASE_LOCK = "RELEASE_POLICY_LOCK_M0_TO_M3"
GATE_TESTNET_ACCOUNT_TYPE = "GATE_TESTNET"
GATE_LIVE_ACCOUNT_TYPE = "GATE_LIVE"


def _clean_account_id(account_id: str) -> str:
    clean = str(account_id or "").strip()
    if not clean or len(clean) > 100:
        raise ValueError("ACCOUNT_REQUIRED")
    return clean


def _json_config(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _account_row(store, account_id: str):
    clean = canonical_account_id(store, _clean_account_id(account_id))
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM accounts WHERE account_id=?",
            (clean,),
        ).fetchone()
    if row is None:
        raise ValueError(f"ACCOUNT_NOT_FOUND: Account '{clean}' is not registered")
    return row


def _default_profile_config(account_id: str, mode: str) -> Dict[str, Any]:
    mode_clean = str(mode).upper().strip()
    if mode_clean not in {"PAPER", "TESTNET", "LIVE"}:
        raise ValueError("GATE_ACCOUNT_MODE_UNSUPPORTED")
    is_testnet = mode_clean == "TESTNET"
    return {
        "venue": GATE_VENUE,
        "gate_account_profile": GATE_PROFILE_VERSION,
        "account_type": GATE_TESTNET_ACCOUNT_TYPE if is_testnet else GATE_LIVE_ACCOUNT_TYPE,
        "provider": GATE_VENUE,
        "environment": "testnet" if is_testnet else "live",
        "execution_mode": "TESTNET" if is_testnet else "LIVE",
        "gate_account_kind": "TESTNET" if is_testnet else "LIVE",
        "gate_market_type": GATE_MARKET_TYPE,
        "settle_currency": GATE_SETTLE_CURRENCY,
        "gate_api_environment": "TESTNET" if is_testnet else "LIVE",
        "gate_api_base_url": GATE_PAPER_API_BASE_URL if is_testnet else GATE_LIVE_API_BASE_URL,
        "execution_adapter": "GATE_TESTNET_API" if is_testnet else "GATE_LIVE_API",
        "credential_scope": account_id,
        "remote_private_read": "EXPLICIT_ONLY",
        "remote_orders": "TESTNET_ONLY" if is_testnet else "LIVE_LOCKED",
        "local_simulator": False,
        "live_execution": "LOCKED_BY_RELEASE_POLICY" if mode_clean == "LIVE" else "TESTNET_ONLY",
        "release_policy": LIVE_RELEASE_LOCK if mode_clean == "LIVE" else None,
        "legacy_aliases": [LEGACY_GATE_PAPER_ACCOUNT_ID] if is_testnet else [],
        "canonical_account_id": GATE_TESTNET_ACCOUNT_ID if is_testnet else account_id,
    }


def _validate_deposit(value: Decimal, field: str) -> Decimal:
    try:
        deposit = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_INVALID") from exc
    if not deposit.is_finite() or deposit < 0:
        raise ValueError(f"{field}_INVALID")
    return deposit


def _ensure_gate_account(
    store,
    *,
    account_id: str,
    mode: str,
    initial_deposit: Decimal,
) -> None:
    clean_id = _clean_account_id(account_id)
    mode_clean = str(mode).upper().strip()
    expected = _default_profile_config(clean_id, mode_clean)
    deposit = _validate_deposit(initial_deposit, "INITIAL_DEPOSIT")

    try:
        row = _account_row(store, clean_id)
    except ValueError as exc:
        if not str(exc).startswith("ACCOUNT_NOT_FOUND:"):
            raise
        AccountLedger(store).create_account(
            clean_id,
            mode=mode_clean,
            currency=GATE_SETTLE_CURRENCY,
            initial_deposit=deposit,
            config=expected,
        )
        return

    existing_mode = str(row["mode"] or "").upper()
    existing_config = _json_config(row["config_json"])
    existing_venue = str(existing_config.get("venue") or ("simulated" if existing_mode == "PAPER" else "gate")).lower()
    if existing_mode != mode_clean or existing_venue != GATE_VENUE:
        raise ValueError(
            f"GATE_ACCOUNT_PROFILE_CONFLICT: '{clean_id}' is already bound to {existing_mode}/{existing_venue}"
        )

    # Add or repair only profile metadata.  Existing capital, timestamps,
    # ledger events, and unrelated user config are preserved.
    merged = dict(existing_config)
    # These keys describe the execution boundary and must be repaired when a
    # prior build wrote LOCAL_PAPER_SIMULATOR or a live URL into the profile.
    # User-owned unrelated config is preserved.
    authoritative_keys = {
        "venue", "gate_account_profile", "account_type", "provider",
        "environment", "execution_mode", "gate_account_kind",
        "gate_market_type", "settle_currency", "gate_api_environment",
        "gate_api_base_url", "execution_adapter", "credential_scope",
        "remote_private_read", "remote_orders", "local_simulator",
        "live_execution", "release_policy",
        "legacy_aliases", "canonical_account_id",
    }
    for key, value in expected.items():
        if key in authoritative_keys or key not in merged or (key == "credential_scope" and not merged[key]):
            merged[key] = value
    if merged != existing_config:
        with store._connect() as db:
            db.execute(
                "UPDATE accounts SET config_json=? WHERE account_id=?",
                (json.dumps(merged, allow_nan=False), clean_id),
            )


def provision_default_gate_accounts(
    store,
    *,
    paper_initial_deposit: Decimal = Decimal("0"),
    live_initial_deposit: Decimal = Decimal("0"),
) -> list[Dict[str, Any]]:
    """Create or reconcile the distinct Gate TestNet and LIVE account rows."""
    # SQLiteStore's market schema is independent from the trading ledger;
    # initialize the ledger tables before the first account lookup.
    AccountLedger(store)
    _ensure_gate_account(
        store,
        account_id=GATE_TESTNET_ACCOUNT_ID,
        mode="TESTNET",
        # The argument is retained for API compatibility, but TestNet equity
        # comes only from Gate's private API.  Never seed local capital for it.
        initial_deposit=Decimal("0"),
    )
    _ensure_gate_account(
        store,
        account_id=GATE_LIVE_ACCOUNT_ID,
        mode="LIVE",
        initial_deposit=live_initial_deposit,
    )
    return list_gate_account_profiles(store)


def get_gate_account_profile(store, account_id: str) -> Dict[str, Any]:
    """Return the authoritative non-secret Gate profile for one account."""
    row = _account_row(store, account_id)
    clean_id = str(row["account_id"])
    mode = str(row["mode"] or "").upper()
    config = _json_config(row["config_json"])
    venue = str(config.get("venue") or ("simulated" if mode == "PAPER" else "gate")).lower()
    if venue != GATE_VENUE:
        raise ValueError(f"ACCOUNT_VENUE_MISMATCH: Account '{clean_id}' is bound to '{venue}'")
    defaults = _default_profile_config(clean_id, mode)
    # Existing installations may have the old LOCAL_PAPER_SIMULATOR marker.
    # Resolve the managed Gate discriminator from the authoritative account
    # mode/profile rather than trusting that stale adapter label.  Provisioning
    # repairs it durably; reads remain safe even before that repair runs.
    if config.get("gate_account_profile") == GATE_PROFILE_VERSION and mode in {"PAPER", "TESTNET", "LIVE"}:
        for key in (
            "account_type", "provider", "environment", "execution_mode",
            "gate_account_kind", "gate_api_environment", "gate_api_base_url",
            "execution_adapter", "remote_orders", "local_simulator", "live_execution",
        ):
            if key not in config or (key == "execution_adapter" and config.get(key) == "LOCAL_PAPER_SIMULATOR"):
                config[key] = defaults[key]
    return {
        "account_id": clean_id,
        "mode": mode,
        "venue": GATE_VENUE,
        "currency": str(row["currency"] or GATE_SETTLE_CURRENCY),
        "initial_deposit": str(row["initial_deposit"]),
        "account_kind": config.get("gate_account_kind", defaults["gate_account_kind"]),
        "account_type": config.get("account_type", defaults["account_type"]),
        "provider": config.get("provider", defaults["provider"]),
        "environment": config.get("environment", defaults["environment"]),
        "execution_mode": config.get("execution_mode", defaults["execution_mode"]),
        "profile_version": config.get("gate_account_profile", defaults["gate_account_profile"]),
        "market_type": config.get("gate_market_type", defaults["gate_market_type"]),
        "settle_currency": config.get("settle_currency", defaults["settle_currency"]),
        "api_environment": config.get("gate_api_environment", defaults["gate_api_environment"]),
        "api_base_url": config.get("gate_api_base_url", defaults["gate_api_base_url"]),
        "execution_adapter": config.get("execution_adapter", defaults["execution_adapter"]),
        "remote_orders": config.get("remote_orders", defaults["remote_orders"]),
        "local_simulator": bool(config.get("local_simulator", defaults["local_simulator"])),
        "credential_scope": config.get("credential_scope", clean_id),
        "remote_private_read": config.get("remote_private_read", "EXPLICIT_ONLY"),
        "live_execution": config.get("live_execution", defaults["live_execution"]),
        "release_policy": config.get("release_policy", defaults["release_policy"]),
        "managed_profile": config.get("gate_account_profile") == GATE_PROFILE_VERSION,
    }


def is_managed_gate_account(store, account_id: str) -> bool:
    try:
        return bool(get_gate_account_profile(store, account_id)["managed_profile"])
    except (ValueError, KeyError):
        return False


def public_gate_account(store, account_id: str) -> Dict[str, Any]:
    """Return profile and credential status, never decrypted secret material."""
    profile = get_gate_account_profile(store, account_id)
    credential_meta = CredentialVault.get_account_metadata(store, profile["account_id"])
    return {
        **profile,
        "credentials": {
            "configured": bool(credential_meta.get("configured")),
            "credential_ref": credential_meta.get("credential_ref", ""),
            "api_key_masked": credential_meta.get("api_key_masked", ""),
            "testnet": bool(credential_meta.get("testnet")),
            "migration_status": credential_meta.get("migration_status", "CLEAN"),
            "updated_at": credential_meta.get("updated_at"),
        },
        "live_status": "LOCKED" if profile["mode"] == "LIVE" else "AVAILABLE",
        "private_api_access": "NOT_ATTEMPTED",
    }


def list_gate_account_profiles(store) -> list[Dict[str, Any]]:
    """List only explicitly managed Gate account profiles in stable order."""
    if not hasattr(store, "_connect"):
        return []
    with store._connect() as db:
        rows = db.execute(
            """
            SELECT account_id, config_json FROM accounts
            ORDER BY account_id ASC
            """
        ).fetchall()
    # SQLite stores both compact and pretty JSON depending on the caller. A
    # formatting change must not make a managed account disappear from the
    # account selector, so inspect the parsed object instead of using LIKE.
    account_ids = []
    for row in rows:
        try:
            config = _json_config(row["config_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if config.get("gate_account_profile") == GATE_PROFILE_VERSION:
            account_ids.append(str(row["account_id"]))
    return [public_gate_account(store, account_id) for account_id in account_ids]


def save_gate_account_credentials(
    store,
    account_id: str,
    api_key: str,
    api_secret: str,
) -> Dict[str, Any]:
    """Persist one account's Gate key pair in its own encrypted slot."""
    profile = get_gate_account_profile(store, account_id)
    if profile["account_type"] not in {GATE_TESTNET_ACCOUNT_TYPE, GATE_LIVE_ACCOUNT_TYPE}:
        raise ValueError("GATE_ACCOUNT_MODE_UNSUPPORTED")
    expected_testnet = profile["account_type"] == GATE_TESTNET_ACCOUNT_TYPE
    meta = CredentialVault.save_account_credentials(
        store,
        profile["account_id"],
        api_key,
        api_secret,
        testnet=expected_testnet,
        venue="gateio",
    )
    # Keep only a non-secret reference in the account profile.  The encrypted
    # material remains solely in secure_account_credentials.
    with store._connect() as db:
        row = db.execute(
            "SELECT config_json FROM accounts WHERE account_id=?",
            (profile["account_id"],),
        ).fetchone()
        config = _json_config(row["config_json"] if row else "{}")
        config["credential_ref"] = meta["credential_ref"]
        db.execute(
            "UPDATE accounts SET config_json=? WHERE account_id=?",
            (json.dumps(config, allow_nan=False), profile["account_id"]),
        )
    return public_gate_account(store, profile["account_id"])


def _redact_validation_value(value: Any, secrets: Tuple[str, ...]) -> Any:
    """Remove submitted secrets from an adapter error before it reaches HTTP/UI."""
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(value, dict):
        return {key: _redact_validation_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_validation_value(item, secrets) for item in value]
    return value


def verify_gate_account_credentials(
    store,
    account_id: str,
    api_key: str,
    api_secret: str,
) -> Dict[str, Any]:
    """Verify one account's credentials with a read-only balance request.

    The candidate pair is never persisted until the remote read succeeds.  The
    adapter is created with the profile's explicit environment and with live
    trading disabled, so this operation cannot place, cancel, or modify an
    order.  A failed/indeterminate check leaves any previously stored pair
    untouched.
    """
    profile = get_gate_account_profile(store, account_id)
    clean_key = str(api_key or "").strip()
    clean_secret = str(api_secret or "").strip()
    if not clean_key or not clean_secret:
        raise ValueError("API_KEY_AND_SECRET_REQUIRED")

    from core.trading.gate_live_client import GateLiveTrader

    candidate = GateLiveTrader(
        clean_key,
        clean_secret,
        testnet=profile["api_environment"] == "TESTNET",
        api_base_url=profile["api_base_url"],
        live_trading_enabled=False,
    )
    try:
        raw_validation = candidate.validate_credentials()
    except Exception as exc:  # Defensive boundary for alternate adapters/test doubles.
        try:
            from core.trading.gate_live_client import _map_gate_error, _error_text
            mapped = _map_gate_error(exc)
            raw_validation = {
                "valid": False,
                "status": "VERIFICATION_FAILED",
                "code": mapped["code"],
                "message_zh": mapped["message_zh"],
                "reason": f"{mapped['code']}: {_error_text(exc)}",
            }
        except Exception:
            raw_validation = {
                "valid": False,
                "status": "VERIFICATION_FAILED",
                "code": "GATE_VERIFICATION_FAILED",
                "message_zh": "Gate 凭证验证失败，请稍后重试。",
                "reason": "GATE_VERIFICATION_FAILED",
            }

    secrets = tuple(sorted({clean_key, clean_secret}, key=len, reverse=True))
    validation = _redact_validation_value(raw_validation, secrets)
    if not isinstance(validation, dict):
        validation = {"valid": False, "reason": "INVALID_VALIDATION_RESPONSE"}
    valid = validation.get("valid") is True
    validation["valid"] = valid
    # Older adapters/test doubles may return only ``valid`` and ``reason``.
    # Preserve the stable legacy invalid/unavailable state for that narrow
    # shape, while the real Gate adapter supplies a more specific mapped
    # ``VERIFICATION_FAILED`` status and error code.
    validation["status"] = "VERIFIED_READ_ONLY" if valid else str(validation.get("status") or "INVALID_OR_UNAVAILABLE")
    validation.setdefault("code", "GATE_CREDENTIALS_VERIFIED" if valid else "GATE_CREDENTIALS_INVALID")
    validation.setdefault(
        "message_zh",
        "Gate TestNet 只读账户验证成功。" if valid and profile["account_type"] == GATE_TESTNET_ACCOUNT_TYPE
        else ("Gate Live 只读账户验证成功。" if valid else "Gate 凭证验证失败，请根据错误码处理后重试。"),
    )
    validation["private_api_access"] = "EXPLICITLY_REQUESTED"
    validation["account_id"] = profile["account_id"]
    validation["api_environment"] = profile["api_environment"]
    validation["validated_at"] = datetime.now(timezone.utc).isoformat()

    if valid:
        account = save_gate_account_credentials(store, profile["account_id"], clean_key, clean_secret)
        return {
            "saved": True,
            "account": account,
            "validation": validation,
            "private_api_access": "EXPLICITLY_REQUESTED",
        }

    return {
        "saved": False,
        "account": public_gate_account(store, profile["account_id"]),
        "validation": validation,
        "private_api_access": "EXPLICITLY_REQUESTED",
    }


def get_gate_account_credentials(
    store, account_id: str
) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    """Return safe metadata plus an internal-only decrypted key pair."""
    profile = get_gate_account_profile(store, account_id)
    metadata = CredentialVault.get_account_metadata(store, profile["account_id"])
    key, secret = CredentialVault.get_account_in_memory_keys(store, profile["account_id"])
    return metadata, key, secret


def build_gate_trader(store, account_id: str):
    """Build the explicitly scoped Gate adapter.

    ``gate_paper`` is Gate TestNet, not a local simulator.  Missing
    credentials therefore return ``None`` and the execution boundary fails
    closed rather than fabricating a fill.
    """
    profile = get_gate_account_profile(store, account_id)
    metadata, key, secret = get_gate_account_credentials(store, profile["account_id"])
    expected_testnet = profile["account_type"] == GATE_TESTNET_ACCOUNT_TYPE
    if not metadata.get("configured") or not key or not secret:
        return None
    # A credential slot with the wrong environment is never reused.  This is
    # the second independent fence after the account profile discriminator.
    if bool(metadata.get("testnet")) != expected_testnet:
        return None
    from core.trading.gate_live_client import GateLiveTrader

    trader = GateLiveTrader(
        key,
        secret,
        testnet=expected_testnet,
        api_base_url=profile["api_base_url"],
        # Gate TestNet is the explicitly requested simulated exchange and may
        # send to TestNet.  Live remains locked by the gateway and this second
        # adapter flag.
        live_trading_enabled=expected_testnet,
    )
    # Expose non-secret scope metadata on the adapter so every downstream
    # caller can audit that the client belongs to the requested account and
    # environment.  Credentials themselves remain in the adapter only for
    # the immediate internal request and are never serialized.
    trader.account_id = profile["account_id"]
    trader.account_type = profile["account_type"]
    trader.provider = profile["provider"]
    trader.environment = profile["environment"]
    trader.credential_scope = profile["credential_scope"]
    return trader


def local_gate_fills(store, account_id: str, symbol: Optional[str] = None, limit: int = 100) -> list[Dict[str, Any]]:
    """Legacy local-fill projection for non-Gate simulator accounts.

    Managed Gate TestNet accounts must never use this projection.  Keeping the
    helper for old ``venue=simulated`` callers avoids a migration-time import
    break while the public Gate routes select the remote adapter by account
    type.
    """
    profile = get_gate_account_profile(store, account_id)
    if profile["account_type"] == GATE_TESTNET_ACCOUNT_TYPE or profile["venue"] != "simulated":
        return []
    target = str(symbol or "").upper().strip()
    with store._connect() as db:
        rows = db.execute(
            """
            SELECT fill_id, order_id, trade_id, symbol, side, quantity, price,
                   fee_amount, fee_currency, event_at, created_at, payload_json
            FROM trade_fills
            WHERE account_id=? AND venue=? AND mode=?
            ORDER BY COALESCE(event_at, created_at) DESC, fill_id DESC
            LIMIT ?
            """,
            (profile["account_id"], GATE_VENUE, "PAPER", max(1, min(int(limit), 500))),
        ).fetchall()
    results: list[Dict[str, Any]] = []
    for row in rows:
        row_symbol = str(row["symbol"] or "")
        if target and target not in row_symbol.upper():
            continue
        payload = _json_config(row["payload_json"])
        try:
            fee = float(Decimal(str(row["fee_amount"] or "0")))
            price = float(Decimal(str(row["price"])))
            amount = float(Decimal(str(row["quantity"])))
        except (InvalidOperation, TypeError, ValueError):
            fee = price = amount = None
        results.append(
            {
                "id": row["trade_id"] or row["fill_id"],
                "order_id": row["order_id"],
                "symbol": row_symbol,
                "datetime": row["event_at"] or row["created_at"],
                "side": str(row["side"] or "UNKNOWN").upper(),
                "price": price,
                "amount": amount,
                "cost": price * amount if price is not None and amount is not None else None,
                "fee_cost": fee,
                "fee_currency": row["fee_currency"] or profile["settle_currency"],
                "fee_evidence_status": "OBSERVED_LOCAL_LEDGER",
                "pnl": payload.get("realized_pnl"),
            }
        )
    return results


__all__ = [
    "GATE_TESTNET_ACCOUNT_ID",
    "LEGACY_GATE_PAPER_ACCOUNT_ID",
    "GATE_LIVE_ACCOUNT_ID",
    "GATE_LIVE_ACCOUNT_TYPE",
    "GATE_LIVE_API_BASE_URL",
    "GATE_MARKET_TYPE",
    "GATE_PAPER_ACCOUNT_ID",
    "GATE_PAPER_API_BASE_URL",
    "GATE_PROFILE_VERSION",
    "GATE_TESTNET_ACCOUNT_TYPE",
    "GATE_SETTLE_CURRENCY",
    "GATE_VENUE",
    "LIVE_RELEASE_LOCK",
    "build_gate_trader",
    "get_gate_account_credentials",
    "get_gate_account_profile",
    "is_managed_gate_account",
    "list_gate_account_profiles",
    "local_gate_fills",
    "provision_default_gate_accounts",
    "public_gate_account",
    "save_gate_account_credentials",
    "verify_gate_account_credentials",
]
