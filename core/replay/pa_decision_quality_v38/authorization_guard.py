"""Fail-closed revocation checks for exhausted V38 model-call authorizations."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

AUTHORIZATION_REVOCATION_SCHEMA_VERSION = "pa-market-only-v38/authorization-revocations-1"
REVOCATION_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "configs" / "research" / "authorizations" / "revocations-v1.json"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuthorizationRevocationError(ValueError):
    """Stable, safe-to-report error for unavailable or revoked authorization."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _revoked_authorizations(path: Path) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorizationRevocationError("AUTHORIZATION_REVOCATION_REGISTRY_UNAVAILABLE") from exc

    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "revocations"}
        or document.get("schema_version") != AUTHORIZATION_REVOCATION_SCHEMA_VERSION
        or not isinstance(document.get("revocations"), list)
    ):
        raise AuthorizationRevocationError("AUTHORIZATION_REVOCATION_REGISTRY_INVALID")

    records: dict[str, dict[str, Any]] = {}
    repository_root = Path(__file__).resolve().parents[3]
    expected_fields = {
        "authorization_id",
        "status",
        "reason_code",
        "maximum_dispatch_intents",
        "observed_dispatch_intents",
        "observed_terminal_results",
        "authorization_record_path",
        "authorization_record_sha256",
        "source_ledger_path",
        "source_ledger_sha256",
    }
    for item in document["revocations"]:
        if (
            not isinstance(item, dict)
            or set(item) != expected_fields
            or not isinstance(item.get("authorization_id"), str)
            or not item["authorization_id"].strip()
            or item.get("status") != "CONSUMED_REVOKED"
            or item.get("reason_code") != "AUTHORIZED_INTENT_CAP_EXHAUSTED"
            or type(item.get("maximum_dispatch_intents")) is not int
            or item["maximum_dispatch_intents"] < 1
            or type(item.get("observed_dispatch_intents")) is not int
            or item["observed_dispatch_intents"] < item["maximum_dispatch_intents"]
            or type(item.get("observed_terminal_results")) is not int
            or not 0 <= item["observed_terminal_results"] <= item["observed_dispatch_intents"]
            or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("authorization_record_path", "source_ledger_path")
            )
            or any(
                PurePosixPath(item[key]).is_absolute()
                or PureWindowsPath(item[key]).is_absolute()
                or ".." in PurePosixPath(item[key]).parts
                or ".." in PureWindowsPath(item[key]).parts
                for key in ("authorization_record_path", "source_ledger_path")
            )
            or any(
                not isinstance(item.get(key), str) or not _SHA256.fullmatch(item[key])
                for key in ("authorization_record_sha256", "source_ledger_sha256")
            )
        ):
            raise AuthorizationRevocationError("AUTHORIZATION_REVOCATION_REGISTRY_INVALID")
        authorization_id = item["authorization_id"]
        if authorization_id in records:
            raise AuthorizationRevocationError("AUTHORIZATION_REVOCATION_REGISTRY_INVALID")

        authorization_path = (repository_root / item["authorization_record_path"]).resolve()
        if not authorization_path.is_relative_to(repository_root):
            raise AuthorizationRevocationError("AUTHORIZATION_REVOCATION_REGISTRY_INVALID")
        try:
            authorization_bytes = authorization_path.read_bytes()
            authorization_record = json.loads(authorization_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthorizationRevocationError("AUTHORIZATION_REVOCATION_REGISTRY_INVALID") from exc
        if (
            hashlib.sha256(authorization_bytes).hexdigest() != item["authorization_record_sha256"]
            or not isinstance(authorization_record, dict)
            or authorization_record.get("authorization_id") != authorization_id
        ):
            raise AuthorizationRevocationError("AUTHORIZATION_REVOCATION_REGISTRY_INVALID")

        records[authorization_id] = item
    return records


def authorization_execution_status(
    authorization_id: str,
    *,
    registry_path: Path = REVOCATION_REGISTRY_PATH,
) -> str:
    if not isinstance(authorization_id, str) or not authorization_id.strip():
        raise AuthorizationRevocationError("RESEARCH_AUTHORIZATION_ID_INVALID")
    records = _revoked_authorizations(registry_path)
    if authorization_id in records:
        return "CONSUMED_REVOKED"
    return "NOT_REVOKED"


def require_authorization_not_revoked(
    authorization_id: str,
    *,
    registry_path: Path = REVOCATION_REGISTRY_PATH,
) -> None:
    if authorization_execution_status(authorization_id, registry_path=registry_path) == "CONSUMED_REVOKED":
        raise AuthorizationRevocationError("RESEARCH_AUTHORIZATION_REVOKED")
