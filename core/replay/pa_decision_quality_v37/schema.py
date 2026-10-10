"""Version-pinned research schemas; independent of mutable production schemas."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

FROZEN_A0_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "configs" / "research" / "schemas" / "a0-v35-action-schema.json"
)
V35_SCHEMA_COMMIT = "2a5d7b9992a7909a6a597917e409ecb0737f9c89"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def load_frozen_a0_schema(path: Path | None = None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Load and authenticate the V35 base schema and its recorded provenance."""
    source_path = path or FROZEN_A0_SCHEMA_PATH
    try:
        record = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("V35_FROZEN_SCHEMA_UNAVAILABLE") from exc
    schema = record.get("schema") if isinstance(record, dict) else None
    provenance = record.get("source") if isinstance(record, dict) else None
    if not isinstance(schema, dict):
        raise TypeError("V35_FROZEN_SCHEMA_INVALID")
    if not isinstance(provenance, dict) or (
        provenance.get("commit") != V35_SCHEMA_COMMIT
        or provenance.get("path") != "core/trading/model_schemas.py"
        or provenance.get("symbol") != "AI_ACTION_SCHEMA"
    ):
        raise ValueError("V35_FROZEN_SCHEMA_PROVENANCE_INVALID")
    digest = hashlib.sha256(canonical_json_bytes(schema)).hexdigest()
    version = f"v35-ai-action-schema/sha256:{digest}"
    if record.get("schema_sha256") != digest:
        raise ValueError("V35_FROZEN_SCHEMA_HASH_MISMATCH")
    if record.get("schema_version") != version:
        raise ValueError("V35_FROZEN_SCHEMA_VERSION_MISMATCH")
    if not isinstance(record.get("frozen_at_utc"), str) or not record["frozen_at_utc"].endswith("Z"):
        raise ValueError("V35_FROZEN_SCHEMA_TIME_INVALID")
    return deepcopy(schema), version, deepcopy(record)
