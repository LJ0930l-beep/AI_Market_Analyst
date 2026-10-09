"""Offline, blinded handoff and collection for V38 human reference labels.

The public rater packet contains only the frozen optimization/validation market
inputs. The decision/partition crosswalk is emitted separately as an escrow
file and is required to validate submissions. This module has no provider,
network, exchange, or order interface.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.replay.pa_decision_quality_v38.blind_labels import (
    load_blind_label_protocol,
    validate_blind_label_batch,
    validate_blind_label_record,
)
from core.replay.pa_decision_quality_v38.dataset import canonical_sha256
from core.replay.pa_decision_quality_v38.market_only import (
    build_market_only_input,
    validate_stored_market_only_input,
)

ROOT = Path(__file__).resolve().parents[3]
PACKET_TEMPLATE = ROOT / "scripts" / "templates" / "v38-blind-label-packet.html"
DATASET_ID = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"
VISIBLE_INPUT_FILE = "optimization-validation-inputs.json"
MANIFEST_FILE = "dataset-manifest.json"
PACKET_SCHEMA_VERSION = "pa-market-only-v38/blind-reference-packet-2"
ESCROW_SCHEMA_VERSION = "pa-market-only-v38/blind-reference-escrow-2"
SUBMISSION_SCHEMA_VERSION = "pa-market-only-v38/blind-reference-submission-1"
RESULT_SCHEMA_VERSION = "pa-market-only-v38/blind-reference-label-batch-1"
LABEL_SCHEMA_VERSION = "pa-market-only-v38/blind-reference-label-1"
TIME_DISPLAY_POLICY_ID = "V38_RATER_RELATIVE_TIMESTAMP_SECONDS_V1"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_RATER_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_INPUT_DOCUMENT_FIELDS = {
    "schema_version", "dataset_kind", "plan_sha256", "partition_policy_sha256",
    "source_database_sha256", "source_manifest_sha256", "partition_scope",
    "decision_points", "outcome_labels_included", "model_outputs_included",
}
_INPUT_POINT_FIELDS = {
    "decision_id", "decision_time", "symbol", "partition", "bars_by_timeframe",
}
_MANIFEST_FILE_FIELDS = {VISIBLE_INPUT_FILE, "untouched-test-sealed-manifest.json"}
_ENUM_FIELDS = (
    "context_regime", "higher_timeframe_bias", "location", "signal_setup",
    "signal_quality", "supported_market_bias", "wait_reference", "target_structure",
)
_LABEL_FIELDS = {
    *_ENUM_FIELDS, "evidence_refs", "target_evidence_refs", "counter_evidence_refs", "rationale",
}
_TARGET_WITH_EVIDENCE = {"PRIOR_SWING", "RANGE_EXTREME", "TREND_MEASURED_MOVE"}
_PUBLIC_ITEM_FIELDS = {
    "packet_item_id", "symbol", "decision_time", "bars_by_timeframe",
    "evidence_catalog", "provenance",
}
_ESCROW_FIELDS = {
    "schema_version", "packet_id", "packet_sha256", "rater_id", "protocol_id",
    "protocol_sha256", "dataset_id", "dataset_manifest_sha256", "visible_input_file_sha256",
    "time_display_policy_id", "crosswalk", "escrow_sha256",
}
_CROSSWALK_FIELDS = {
    "packet_item_id", "decision_id", "partition", "decision_time", "market_input_sha256",
}
_SUBMISSION_FIELDS = {
    "schema_version", "packet_id", "packet_sha256", "rater_id", "protocol_id",
    "protocol_sha256", "dataset_id", "dataset_manifest_sha256", "time_display_policy_id", "annotations",
}
_SUBMISSION_ANNOTATION_FIELDS = {"packet_item_id", "labelled_at", "label"}


class BlindLabelPacketError(ValueError):
    """Stable error code for unsafe, stale, or malformed label handoff artifacts."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(condition: bool, code: str) -> None:
    if not condition:
        raise BlindLabelPacketError(code)


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BlindLabelPacketError(code) from exc
    if not isinstance(value, dict):
        raise BlindLabelPacketError(code)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BlindLabelPacketError("BLIND_PACKET_VISIBLE_INPUT_UNAVAILABLE") from exc
    return digest.hexdigest()


def load_v38_visible_dataset(dataset_directory: Path) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]], str, str,
]:
    """Load and rebind only the visible input document; never open sealed-test files."""
    directory = Path(dataset_directory)
    manifest = _read_json(directory / MANIFEST_FILE, "BLIND_PACKET_MANIFEST_INVALID")
    _fail(
        manifest.get("schema_version") == "pa-market-only-v38/stratified-purged-dataset-manifest-1"
        and manifest.get("dataset_id") == DATASET_ID,
        "BLIND_PACKET_DATASET_INVALID",
    )
    claimed_manifest_sha = manifest.get("manifest_sha256")
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest_sha = canonical_sha256(manifest_body)
    _fail(claimed_manifest_sha == manifest_sha, "BLIND_PACKET_MANIFEST_HASH_MISMATCH")
    manifest_files = manifest.get("files")
    _fail(
        isinstance(manifest_files, dict)
        and set(manifest_files) == _MANIFEST_FILE_FIELDS
        and manifest.get("optimization_validation_input_count") == 54
        and manifest.get("untouched_test_hash_only_count") == 18
        and manifest.get("partition_counts") == {
            "optimization": 36, "validation": 18, "untouched_test": 18,
        }
        and manifest.get("untouched_test_payloads_included") is False
        and manifest.get("outcome_labels_included") is False
        and manifest.get("model_outputs") == 0
        and manifest.get("gemini_research_calls_used") == 0
        and manifest.get("orders_created") == 0,
        "BLIND_PACKET_MANIFEST_SCOPE_INVALID",
    )

    visible_path = directory / VISIBLE_INPUT_FILE
    visible_sha = _sha256_file(visible_path)
    _fail(
        manifest["files"].get(VISIBLE_INPUT_FILE) == visible_sha,
        "BLIND_PACKET_VISIBLE_INPUT_HASH_MISMATCH",
    )
    inputs = _read_json(visible_path, "BLIND_PACKET_VISIBLE_INPUT_INVALID")
    _fail(
        set(inputs) == _INPUT_DOCUMENT_FIELDS
        and inputs.get("schema_version") == "pa-market-only-v38/stratified-purged-market-input-dataset-1"
        and inputs.get("partition_scope") == ["optimization", "validation"]
        and inputs.get("outcome_labels_included") is False
        and inputs.get("model_outputs_included") is False,
        "BLIND_PACKET_VISIBLE_INPUT_SCOPE_INVALID",
    )

    protocol, protocol_sha = load_blind_label_protocol()
    manifest_bindings = protocol.get("eligible_dataset_manifest_sha256")
    _fail(
        canonical_sha256(protocol) == protocol_sha
        and protocol.get("eligible_datasets") == [DATASET_ID]
        and isinstance(manifest_bindings, dict)
        and manifest_bindings.get(DATASET_ID) == manifest_sha,
        "BLIND_PACKET_PROTOCOL_BINDING_INVALID",
    )
    bindings_container = protocol.get("eligible_input_bindings")
    _fail(isinstance(bindings_container, dict), "BLIND_PACKET_PROTOCOL_BINDINGS_INVALID")
    bindings_rows = bindings_container.get(DATASET_ID)
    _fail(isinstance(bindings_rows, list) and len(bindings_rows) == 54,
          "BLIND_PACKET_PROTOCOL_BINDINGS_INVALID")
    bindings: dict[str, dict[str, Any]] = {}
    for row in bindings_rows:
        _fail(
            isinstance(row, dict)
            and set(row) == {"decision_id", "market_input_sha256", "partition"}
            and isinstance(row.get("decision_id"), str)
            and _TOKEN.fullmatch(row["decision_id"]) is not None
            and isinstance(row.get("market_input_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", row["market_input_sha256"]) is not None
            and isinstance(row.get("partition"), str)
            and row["partition"] in {"optimization", "validation"}
            and row["decision_id"] not in bindings,
            "BLIND_PACKET_PROTOCOL_BINDINGS_INVALID",
        )
        bindings[row["decision_id"]] = row

    points = inputs.get("decision_points")
    _fail(isinstance(points, list) and len(points) == 54,
          "BLIND_PACKET_VISIBLE_INPUT_COUNT_INVALID")
    points_by_id: dict[str, dict[str, Any]] = {}
    market_inputs: dict[str, dict[str, Any]] = {}
    partition_counts = {"optimization": 0, "validation": 0}
    for point in points:
        _fail(
            isinstance(point, dict) and set(point) == _INPUT_POINT_FIELDS
            and isinstance(point.get("partition"), str)
            and point["partition"] in partition_counts
            and isinstance(point.get("decision_id"), str)
            and point["decision_id"] not in points_by_id,
            "BLIND_PACKET_VISIBLE_INPUT_POINT_INVALID",
        )
        decision_id = point["decision_id"]
        market_binding = bindings.get(decision_id)
        _fail(
            market_binding is not None and market_binding.get("partition") == point["partition"],
            "BLIND_PACKET_INPUT_NOT_REGISTERED",
        )
        try:
            market_input = build_market_only_input(point)
        except (TypeError, ValueError) as exc:
            raise BlindLabelPacketError("BLIND_PACKET_MARKET_INPUT_INVALID") from exc
        _fail(
            validate_stored_market_only_input(market_input)
            and market_input.get("market_input_sha256") == market_binding.get("market_input_sha256"),
            "BLIND_PACKET_MARKET_INPUT_BINDING_MISMATCH",
        )
        points_by_id[decision_id] = point
        market_inputs[decision_id] = market_input
        partition_counts[point["partition"]] += 1
    _fail(
        set(points_by_id) == set(bindings)
        and partition_counts == {"optimization": 36, "validation": 18},
        "BLIND_PACKET_VISIBLE_INPUT_BINDINGS_MISMATCH",
    )
    return manifest, inputs, points_by_id, market_inputs, manifest_sha, visible_sha


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BlindLabelPacketError("BLIND_PACKET_MARKET_TIME_INVALID")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise BlindLabelPacketError("BLIND_PACKET_MARKET_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BlindLabelPacketError("BLIND_PACKET_MARKET_TIME_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BlindLabelPacketError("BLIND_PACKET_MARKET_TIME_INVALID")
    return parsed.astimezone(UTC)


def _bar_reference(timeframe: str, row: dict[str, Any]) -> str:
    """Mirror the V36 CausalBar reference from exact source bar identity."""
    start = _utc_stamp(_parse_utc(row.get("bar_start")))
    end = _utc_stamp(_parse_utc(row.get("bar_end")))
    source = row.get("source")
    if not isinstance(source, str) or not source.strip():
        raise BlindLabelPacketError("BLIND_PACKET_BAR_PROVENANCE_MISSING")
    seed = f"{timeframe}|{start}|{end}|{source.strip()}"
    return "bar:" + timeframe + ":" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _relative_timestamp(value: datetime, decision_time: datetime) -> str:
    """Render time relative to T0 so calendar dates do not reveal the split."""
    delta = value - decision_time
    total_microseconds = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if total_microseconds == 0:
        return "T0"
    sign = "+" if total_microseconds > 0 else "-"
    seconds, micros = divmod(abs(total_microseconds), 1_000_000)
    fraction = f".{micros:06d}".rstrip("0") if micros else ""
    return f"T{sign}{seconds}{fraction}s"


def _public_item(
    packet_item_id: str,
    point: dict[str, Any],
    market_input: dict[str, Any],
) -> dict[str, Any]:
    """Create a reviewer view with no decision ID, partition, arm, or derived label hints."""
    valid_refs = set(market_input["evidence_refs"])
    evidence_catalog: list[dict[str, str]] = []
    bars_by_timeframe: dict[str, list[dict[str, Any]]] = {}
    decision_time = _parse_utc(market_input["decision_time"])
    context_frames = market_input["market_context"].get("frames")
    if not isinstance(context_frames, dict):
        raise BlindLabelPacketError("BLIND_PACKET_MARKET_CONTEXT_INVALID")
    for timeframe in ("15m", "5m", "1h", "4h"):
        frame = context_frames.get(timeframe)
        if not isinstance(frame, dict) or not isinstance(frame.get("bar_count"), int):
            raise BlindLabelPacketError("BLIND_PACKET_MARKET_CONTEXT_INVALID")
        causal_rows = []
        for row in point["bars_by_timeframe"][timeframe]:
            available_at = _parse_utc(row.get("available_at"))
            if available_at < decision_time:
                causal_rows.append(row)
        if len(causal_rows) != frame["bar_count"]:
            raise BlindLabelPacketError("BLIND_PACKET_CAUSAL_BAR_COUNT_MISMATCH")
        public_bars: list[dict[str, Any]] = []
        for raw in causal_rows:
            bar_start = _utc_stamp(_parse_utc(raw["bar_start"]))
            bar_end = _utc_stamp(_parse_utc(raw["bar_end"]))
            available_at = _utc_stamp(_parse_utc(raw["available_at"]))
            calculated_ref = _bar_reference(timeframe, raw)
            ref = calculated_ref if calculated_ref in valid_refs else None
            public_bar = {
                "bar_start": _relative_timestamp(_parse_utc(bar_start), decision_time),
                "bar_end": _relative_timestamp(_parse_utc(bar_end), decision_time),
                "available_at": _relative_timestamp(_parse_utc(available_at), decision_time),
                "open": raw["open"],
                "high": raw["high"],
                "low": raw["low"],
                "close": raw["close"],
                "volume": raw["volume"],
                "volume_unit": raw.get("volume_unit", "UNKNOWN"),
                "source": raw["source"],
                "evidence_ref": ref,
            }
            public_bars.append(public_bar)
            if ref is not None:
                evidence_catalog.append({
                    "evidence_ref": ref,
                    "timeframe": timeframe,
                    "bar_end": _relative_timestamp(_parse_utc(bar_end), decision_time),
                })
        bars_by_timeframe[timeframe] = public_bars

    evidence_catalog.sort(key=lambda row: (row["timeframe"], row["bar_end"], row["evidence_ref"]))
    _fail(
        {row["evidence_ref"] for row in evidence_catalog} == valid_refs,
        "BLIND_PACKET_EVIDENCE_REF_MAPPING_INVALID",
    )
    provenance = market_input["provenance"]
    item = {
        "packet_item_id": packet_item_id,
        "symbol": market_input["symbol"],
        "decision_time": "T0",
        "bars_by_timeframe": bars_by_timeframe,
        "evidence_catalog": evidence_catalog,
        "provenance": {
            "source_exchange": provenance["source_exchange"],
            "price_evidence_grade": provenance["price_evidence_grade"],
            "availability_evidence_grade": provenance["availability_evidence_grade"],
            "availability_basis": provenance["availability_basis"],
            "volume_unit": provenance["volume_unit"],
        },
    }
    _fail(set(item) == _PUBLIC_ITEM_FIELDS,
          "BLIND_PACKET_PUBLIC_ITEM_FIELDS_INVALID")
    return item


def create_blind_annotation_packet(
    dataset_directory: Path,
    *,
    rater_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a randomized public packet and separate decision/partition escrow."""
    if not isinstance(rater_id, str) or _RATER_TOKEN.fullmatch(rater_id) is None:
        raise BlindLabelPacketError("BLIND_PACKET_RATER_ID_INVALID")
    _manifest, _inputs, points_by_id, market_inputs, manifest_sha, visible_sha = (
        load_v38_visible_dataset(dataset_directory)
    )
    protocol, protocol_sha = load_blind_label_protocol()
    item_bindings = protocol["eligible_input_bindings"][DATASET_ID]
    by_decision = {row["decision_id"]: row for row in item_bindings}
    selected_ids = list(points_by_id)
    secrets.SystemRandom().shuffle(selected_ids)
    packet_id = uuid.uuid4().hex
    crosswalk: list[dict[str, Any]] = []
    public_items: list[dict[str, Any]] = []
    for decision_id in selected_ids:
        point = points_by_id[decision_id]
        market_input = market_inputs[decision_id]
        binding = by_decision[decision_id]
        if (market_input["market_input_sha256"] != binding["market_input_sha256"]
                or market_input["partition"] != binding["partition"]):
            raise BlindLabelPacketError("BLIND_PACKET_INPUT_NOT_REGISTERED")
        packet_item_id = secrets.token_hex(16)
        public_items.append(_public_item(packet_item_id, point, market_input))
        crosswalk.append({
            "packet_item_id": packet_item_id,
            "decision_id": decision_id,
            "partition": market_input["partition"],
            "decision_time": market_input["decision_time"],
            "market_input_sha256": market_input["market_input_sha256"],
        })

    label_contract = protocol.get("label_fields", {})
    enum_options = {
        field: label_contract.get(field)
        for field in _ENUM_FIELDS
    }
    if any(not isinstance(values, list) or not values for values in enum_options.values()):
        raise BlindLabelPacketError("BLIND_PACKET_LABEL_CONTRACT_INVALID")
    packet: dict[str, Any] = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "packet_id": packet_id,
        "rater_id": rater_id,
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_sha,
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": manifest_sha,
        "time_display_policy_id": TIME_DISPLAY_POLICY_ID,
        "market_input_schema_version": protocol["evidence_contract"]["market_input_schema_version"],
        "blinding": {
            "model_output_visible": False,
            "future_outcomes_visible": False,
            "experiment_arm_visible": False,
            "untouched_test_payload_visible": False,
            "partition_visible": False,
            "decision_id_visible": False,
        },
        "reviewer_instructions": [
            "Judge only the supplied historical market bars as of the displayed decision time.",
            "T0 is the decision point; all displayed bar times are relative seconds. Original calendar timestamps are withheld.",
            "The +60 second availability time is an assumed proxy, not observed historical Gate receipt time.",
            "Historical news, funding, account state, order book, and future outcomes were not provided; treat them as UNKNOWN.",
            "Use UNKNOWN or UNCERTAIN when evidence does not support a definite label. Cite only listed frozen bar references.",
            "This is a market-structure annotation, not a trade proposal, execution decision, or profitability label.",
        ],
        "label_field_order": list(_ENUM_FIELDS),
        "label_enum_options": enum_options,
        "evidence_ref_roles": ["evidence_refs", "target_evidence_refs", "counter_evidence_refs"],
        "items": public_items,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    escrow: dict[str, Any] = {
        "schema_version": ESCROW_SCHEMA_VERSION,
        "packet_id": packet_id,
        "packet_sha256": packet["packet_sha256"],
        "rater_id": rater_id,
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_sha,
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": manifest_sha,
        "visible_input_file_sha256": visible_sha,
        "time_display_policy_id": TIME_DISPLAY_POLICY_ID,
        "crosswalk": crosswalk,
    }
    escrow["escrow_sha256"] = canonical_sha256(escrow)
    # Confirm the output itself cannot accidentally reveal internal identifiers.
    public_bytes = json.dumps(packet, ensure_ascii=False, sort_keys=True)
    for row in crosswalk:
        if row["decision_id"] in public_bytes or row["partition"] in public_bytes:
            raise BlindLabelPacketError("BLIND_PACKET_BLINDING_LEAK")
    return packet, escrow


def render_blind_annotation_html(packet: dict[str, Any], template_path: Path | None = None) -> str:
    """Render the packet into one self-contained, network-disabled review page."""
    if not isinstance(packet, dict) or packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        raise BlindLabelPacketError("BLIND_PACKET_INVALID")
    packet_body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if packet.get("packet_sha256") != canonical_sha256(packet_body):
        raise BlindLabelPacketError("BLIND_PACKET_HASH_MISMATCH")
    try:
        template = Path(template_path or PACKET_TEMPLATE).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BlindLabelPacketError("BLIND_PACKET_TEMPLATE_UNAVAILABLE") from exc
    placeholder = "__V38_PACKET_BASE64__"
    if template.count(placeholder) != 1:
        raise BlindLabelPacketError("BLIND_PACKET_TEMPLATE_INVALID")
    encoded = base64.b64encode(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return template.replace(placeholder, encoded)


def _validate_escrow(escrow: Any) -> None:
    if not isinstance(escrow, dict) or set(escrow) != _ESCROW_FIELDS:
        raise BlindLabelPacketError("BLIND_SUBMISSION_ESCROW_INVALID")
    body = {key: value for key, value in escrow.items() if key != "escrow_sha256"}
    if escrow.get("schema_version") != ESCROW_SCHEMA_VERSION or escrow.get("escrow_sha256") != canonical_sha256(body):
        raise BlindLabelPacketError("BLIND_SUBMISSION_ESCROW_HASH_MISMATCH")
    if escrow.get("time_display_policy_id") != TIME_DISPLAY_POLICY_ID:
        raise BlindLabelPacketError("BLIND_SUBMISSION_TIME_DISPLAY_POLICY_INVALID")
    crosswalk = escrow.get("crosswalk")
    if not isinstance(crosswalk, list) or len(crosswalk) != 54:
        raise BlindLabelPacketError("BLIND_SUBMISSION_ESCROW_CROSSWALK_INVALID")
    if any(not isinstance(row, dict) or set(row) != _CROSSWALK_FIELDS for row in crosswalk):
        raise BlindLabelPacketError("BLIND_SUBMISSION_ESCROW_CROSSWALK_INVALID")
    if any(
        not isinstance(row.get("packet_item_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", row["packet_item_id"]) is None
        or not isinstance(row.get("decision_id"), str)
        or not isinstance(row.get("decision_time"), str)
        or not isinstance(row.get("market_input_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", row["market_input_sha256"]) is None
        or row.get("partition") not in {"optimization", "validation"}
        for row in crosswalk
    ):
        raise BlindLabelPacketError("BLIND_SUBMISSION_ESCROW_CROSSWALK_INVALID")
    if len({row["packet_item_id"] for row in crosswalk}) != 54:
        raise BlindLabelPacketError("BLIND_SUBMISSION_ESCROW_CROSSWALK_INVALID")


def import_blind_label_submissions(
    dataset_directory: Path,
    escrow_submission_pairs: Iterable[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Rebind two or more complete blinded packets to the frozen inputs and validate labels."""
    _manifest, _inputs, points_by_id, market_inputs, manifest_sha, visible_sha = (
        load_v38_visible_dataset(dataset_directory)
    )
    protocol, protocol_sha = load_blind_label_protocol()
    pairs = list(escrow_submission_pairs)
    if len(pairs) < 2:
        raise BlindLabelPacketError("BLIND_SUBMISSION_TWO_RATERS_REQUIRED")

    records: list[dict[str, Any]] = []
    seen_packet_ids: set[str] = set()
    seen_rater_ids: set[str] = set()
    expected_ids = set(points_by_id)
    for escrow, submission in pairs:
        _validate_escrow(escrow)
        if not isinstance(submission, dict) or set(submission) != _SUBMISSION_FIELDS:
            raise BlindLabelPacketError("BLIND_SUBMISSION_FIELDS_INVALID")
        if (escrow["dataset_id"] != DATASET_ID
                or escrow["dataset_manifest_sha256"] != manifest_sha
                or escrow["visible_input_file_sha256"] != visible_sha
                or escrow["time_display_policy_id"] != TIME_DISPLAY_POLICY_ID
                or escrow["protocol_id"] != protocol.get("protocol_id")
                or escrow["protocol_sha256"] != protocol_sha):
            raise BlindLabelPacketError("BLIND_SUBMISSION_ESCROW_STALE")
        rater_id = escrow.get("rater_id")
        packet_id = escrow.get("packet_id")
        if (not isinstance(rater_id, str) or _RATER_TOKEN.fullmatch(rater_id) is None
                or not isinstance(packet_id, str) or re.fullmatch(r"[0-9a-f]{32}", packet_id) is None
                or rater_id in seen_rater_ids or packet_id in seen_packet_ids):
            raise BlindLabelPacketError("BLIND_SUBMISSION_RATER_OR_PACKET_DUPLICATE")
        seen_rater_ids.add(rater_id)
        seen_packet_ids.add(packet_id)
        if (submission.get("schema_version") != SUBMISSION_SCHEMA_VERSION
                or submission.get("packet_id") != packet_id
                or submission.get("packet_sha256") != escrow.get("packet_sha256")
                or submission.get("rater_id") != rater_id
                or submission.get("protocol_id") != protocol.get("protocol_id")
                or submission.get("protocol_sha256") != protocol_sha
                or submission.get("dataset_id") != DATASET_ID
                or submission.get("dataset_manifest_sha256") != manifest_sha
                or submission.get("time_display_policy_id") != TIME_DISPLAY_POLICY_ID):
            raise BlindLabelPacketError("BLIND_SUBMISSION_PACKET_BINDING_INVALID")
        annotations = submission.get("annotations")
        if not isinstance(annotations, list) or len(annotations) != 54:
            raise BlindLabelPacketError("BLIND_SUBMISSION_COVERAGE_INCOMPLETE")
        crosswalk = {row["packet_item_id"]: row for row in escrow["crosswalk"]}
        item_ids: set[str] = set()
        for annotation in annotations:
            if (not isinstance(annotation, dict)
                    or set(annotation) != _SUBMISSION_ANNOTATION_FIELDS):
                raise BlindLabelPacketError("BLIND_SUBMISSION_ANNOTATION_FIELDS_INVALID")
            item_id = annotation.get("packet_item_id")
            if not isinstance(item_id, str) or re.fullmatch(r"[0-9a-f]{32}", item_id) is None:
                raise BlindLabelPacketError("BLIND_SUBMISSION_ITEM_BINDING_INVALID")
            mapping = crosswalk.get(item_id) if isinstance(item_id, str) else None
            if mapping is None or item_id in item_ids:
                raise BlindLabelPacketError("BLIND_SUBMISSION_ITEM_BINDING_INVALID")
            item_ids.add(item_id)
            decision_id = mapping["decision_id"]
            if decision_id not in expected_ids or decision_id not in market_inputs:
                raise BlindLabelPacketError("BLIND_SUBMISSION_ITEM_BINDING_INVALID")
            market_input = market_inputs[decision_id]
            if (mapping["market_input_sha256"] != market_input["market_input_sha256"]
                    or mapping["decision_time"] != market_input["decision_time"]
                    or mapping["partition"] != market_input["partition"]):
                raise BlindLabelPacketError("BLIND_SUBMISSION_INPUT_BINDING_INVALID")
            label = annotation.get("label")
            if not isinstance(label, dict) or set(label) != _LABEL_FIELDS:
                raise BlindLabelPacketError("BLIND_SUBMISSION_LABEL_FIELDS_INVALID")
            record = {
                "schema_version": LABEL_SCHEMA_VERSION,
                "protocol_id": protocol["protocol_id"],
                "protocol_sha256": protocol_sha,
                "label_id": f"label:{packet_id[:16]}:{item_id}",
                "dataset_id": DATASET_ID,
                "dataset_manifest_sha256": manifest_sha,
                "market_input_sha256": market_input["market_input_sha256"],
                "decision_id": decision_id,
                "partition": market_input["partition"],
                "decision_time": market_input["decision_time"],
                "reviewer_id": rater_id,
                "labelled_at": annotation.get("labelled_at"),
                "blinding": {
                    "model_output_visible": False,
                    "future_outcomes_visible": False,
                    "experiment_arm_visible": False,
                    "untouched_test_payload_visible": False,
                },
                "label": label,
            }
            errors = validate_blind_label_record(record, market_input)
            if errors:
                raise BlindLabelPacketError("BLIND_SUBMISSION_LABEL_INVALID:" + ",".join(errors))
            records.append(record)
        if item_ids != set(crosswalk) or len(item_ids) != 54:
            raise BlindLabelPacketError("BLIND_SUBMISSION_COVERAGE_INCOMPLETE")

    errors = validate_blind_label_batch(records, market_inputs, required_decision_ids=expected_ids)
    if errors:
        raise BlindLabelPacketError("BLIND_SUBMISSION_BATCH_INVALID:" + ",".join(errors))
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_sha,
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": manifest_sha,
        "rater_count": len(seen_rater_ids),
        "decision_count": len(expected_ids),
        "annotation_count": len(records),
        "validation_status": "VALIDATED_MINIMUM_RATER_COVERAGE",
        "annotations": records,
    }


def write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    """Write a new artifact only; never overwrite an existing packet, escrow, or label batch."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except FileExistsError as exc:
        raise BlindLabelPacketError("BLIND_PACKET_OUTPUT_EXISTS") from exc


def write_text_exclusive(path: Path, text: str) -> None:
    """Write a new UTF-8 text artifact only; never overwrite an existing file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except FileExistsError as exc:
        raise BlindLabelPacketError("BLIND_PACKET_OUTPUT_EXISTS") from exc
