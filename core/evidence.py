"""Immutable evidence bundles for institutional research and AI decisions.

An evidence bundle freezes the exact point-in-time inputs used by a run.  It
does not manufacture missing values: an unavailable source, model digest, or
reference remains explicitly marked as missing/unknown in the persisted row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        point = value
    else:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence timestamps must be ISO timestamps") from exc
    if point.tzinfo is None:
        raise ValueError("evidence timestamps must be timezone-aware")
    return point.astimezone(timezone.utc)


def _iso(value: datetime | str) -> str:
    return _utc(value).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical(value))


@dataclass(frozen=True)
class EvidenceBundle:
    """Frozen, serializable point-in-time evidence context."""

    bundle_id: str
    dataset_id: str
    as_of: str
    expires_at: str
    frozen_at: str
    input_hash: str
    payload: Mapping[str, Any]
    missing: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    version: str = "evidence_bundle_v1"
    status: str = "FROZEN"

    @classmethod
    def freeze(
        cls,
        *,
        dataset_id: str,
        as_of: datetime | str,
        expires_at: datetime | str,
        payload: Mapping[str, Any],
        bundle_id: str | None = None,
        missing: tuple[str, ...] | list[str] = (),
        references: tuple[str, ...] | list[str] = (),
        frozen_at: datetime | str | None = None,
        version: str = "evidence_bundle_v1",
    ) -> "EvidenceBundle":
        if not str(dataset_id).strip():
            raise ValueError("dataset_id is required")
        if not isinstance(payload, Mapping):
            raise ValueError("evidence payload must be an object")
        as_of_text = _iso(as_of)
        expires_text = _iso(expires_at)
        as_of_dt = _utc(as_of_text)
        expires_dt = _utc(expires_text)
        if expires_dt <= as_of_dt:
            raise ValueError("evidence expires_at must be after as_of")
        clean_missing = tuple(sorted({str(item).strip() for item in missing if str(item).strip()}))
        clean_refs = tuple(sorted({str(item).strip() for item in references if str(item).strip()}))
        frozen_text = _iso(frozen_at or datetime.now(timezone.utc))
        body = {
            "dataset_id": str(dataset_id).strip(),
            "as_of": as_of_text,
            "expires_at": expires_text,
            "payload": _json_copy(dict(payload)),
            "missing": list(clean_missing),
            "references": list(clean_refs),
            "version": str(version),
        }
        input_hash = _hash(body)
        resolved_id = str(bundle_id or f"bundle_{input_hash[:24]}").strip()
        if not resolved_id:
            raise ValueError("bundle_id is required")
        return cls(
            bundle_id=resolved_id,
            dataset_id=body["dataset_id"],
            as_of=as_of_text,
            expires_at=expires_text,
            frozen_at=frozen_text,
            input_hash=input_hash,
            payload=body["payload"],
            missing=clean_missing,
            references=clean_refs,
            version=str(version),
            status="FROZEN",
        )

    def status_at(self, now: datetime | str | None = None) -> str:
        point = _utc(now or datetime.now(timezone.utc))
        return "EXPIRED" if point >= _utc(self.expires_at) else self.status

    def validate_references(self, required: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        available = set(self.references)
        return tuple(sorted({str(item) for item in required if str(item) not in available}))

    def to_dict(self, *, now: datetime | str | None = None) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "dataset_id": self.dataset_id,
            "as_of": self.as_of,
            "expires_at": self.expires_at,
            "frozen_at": self.frozen_at,
            "input_hash": self.input_hash,
            "payload": _json_copy(self.payload),
            "missing": list(self.missing),
            "references": list(self.references),
            "version": self.version,
            "status": self.status_at(now),
        }


def validate_weight_digest(value: Any) -> str | None:
    """Return only a real SHA-256 digest; model names are never accepted."""

    candidate = str(value or "").strip().lower()
    return candidate if _SHA256_RE.fullmatch(candidate) else None


def model_weight_digest(provider: Any, *, health_result: Mapping[str, Any] | None = None) -> tuple[str | None, str]:
    """Read an adapter-provided weight digest without hashing model identity."""

    if provider is None:
        return None, "NOT_CONFIGURED"
    for name in ("weight_digest", "model_weight_digest", "digest"):
        digest = validate_weight_digest(getattr(provider, name, None))
        if digest:
            return digest, "OBSERVED_PROVIDER_DIGEST"
    result = health_result
    if result is None:
        health = getattr(provider, "health", None)
        if callable(health):
            try:
                result = health()
            except Exception:
                result = {}
    if isinstance(result, Mapping):
        for name in ("weight_digest", "model_weight_digest", "digest"):
            digest = validate_weight_digest(result.get(name))
            if digest:
                return digest, "OBSERVED_PROVIDER_DIGEST"
    return None, "UNKNOWN_NOT_PROVIDED"


def persist_evidence_bundle(store: Any, bundle: EvidenceBundle) -> dict[str, Any]:
    """Persist once, rejecting a conflicting reuse of an immutable id."""

    payload = bundle.to_dict()
    with store._connect() as db:
        row = db.execute(
            "SELECT input_hash, payload_json, status FROM evidence_bundles WHERE bundle_id=?",
            (bundle.bundle_id,),
        ).fetchone()
        if row is not None:
            if str(row["input_hash"]) != bundle.input_hash:
                raise ValueError("EVIDENCE_BUNDLE_ID_CONFLICT")
            return {**payload, "persisted": False, "status": str(row["status"])}
        db.execute(
            """INSERT INTO evidence_bundles(
                bundle_id, as_of, expires_at, frozen_at, input_hash,
                payload_json, missing_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bundle.bundle_id,
                bundle.as_of,
                bundle.expires_at,
                bundle.frozen_at,
                bundle.input_hash,
                _canonical(bundle.payload),
                _canonical(list(bundle.missing)),
                bundle.status,
            ),
        )
    return {**payload, "persisted": True}


__all__ = [
    "EvidenceBundle",
    "model_weight_digest",
    "persist_evidence_bundle",
    "validate_weight_digest",
]
