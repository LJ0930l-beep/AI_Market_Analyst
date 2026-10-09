from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v38 import blind_label_packets, blind_labels
from core.replay.pa_decision_quality_v38.blind_label_packets import (
    BlindLabelPacketError,
    create_blind_annotation_packet,
    import_blind_label_submissions,
    render_blind_annotation_html,
    write_json_exclusive,
)
from core.replay.pa_decision_quality_v38.dataset import canonical_sha256
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input

DATASET_ID = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"
_VISIBLE_NAME = "optimization-validation-inputs.json"
_MANIFEST_NAME = "dataset-manifest.json"


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


@pytest.fixture
def frozen_visible_dataset(tmp_path, market_point_factory, monkeypatch):
    points = []
    for index in range(54):
        if index < 36:
            partition = "optimization"
            decision = datetime(2025, 10, 15, 12, tzinfo=UTC) + timedelta(days=index)
            local_index = index
        else:
            partition = "validation"
            local_index = index - 36
            decision = datetime(2026, 4, 15, 12, tzinfo=UTC) + timedelta(days=local_index)
        symbol = "BTCUSDT" if index % 2 == 0 else "ETHUSDT"
        points.append(market_point_factory(
            decision=decision,
            decision_id=f"fixture-v38-{partition}-{index:03d}",
            partition=partition,
            symbol=symbol,
        ))

    inputs = {
        "schema_version": "pa-market-only-v38/stratified-purged-market-input-dataset-1",
        "dataset_kind": "MARKET_ONLY_CAUSAL_STRATIFIED_PURGED_PAIRED_CONTEXTS_NO_LABELS",
        "plan_sha256": "a" * 64,
        "partition_policy_sha256": "b" * 64,
        "source_database_sha256": "c" * 64,
        "source_manifest_sha256": "d" * 64,
        "partition_scope": ["optimization", "validation"],
        "decision_points": points,
        "outcome_labels_included": False,
        "model_outputs_included": False,
    }
    dataset_directory = tmp_path / "dataset"
    dataset_directory.mkdir()
    visible_path = dataset_directory / _VISIBLE_NAME
    _write_json(visible_path, inputs)
    visible_sha = blind_label_packets._sha256_file(visible_path)
    manifest = {
        "schema_version": "pa-market-only-v38/stratified-purged-dataset-manifest-1",
        "dataset_id": DATASET_ID,
        "partition_counts": {"optimization": 36, "validation": 18, "untouched_test": 18},
        "optimization_validation_input_count": 54,
        "untouched_test_hash_only_count": 18,
        "untouched_test_payloads_included": False,
        "outcome_labels_included": False,
        "model_outputs": 0,
        "gemini_research_calls_used": 0,
        "orders_created": 0,
        "files": {
            _VISIBLE_NAME: visible_sha,
            "untouched-test-sealed-manifest.json": "e" * 64,
        },
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    _write_json(dataset_directory / _MANIFEST_NAME, manifest)
    # Deliberately do not create the sealed file. Packet creation must not open it.
    assert not (dataset_directory / "untouched-test-sealed-manifest.json").exists()

    real_protocol, _ = blind_labels.load_blind_label_protocol()
    protocol = copy.deepcopy(real_protocol)
    protocol["eligible_dataset_manifest_sha256"][DATASET_ID] = manifest["manifest_sha256"]
    protocol["eligible_input_bindings"][DATASET_ID] = sorted(
        [
            {
                "decision_id": point["decision_id"],
                "market_input_sha256": build_market_only_input(point)["market_input_sha256"],
                "partition": point["partition"],
            }
            for point in points
        ],
        key=lambda row: row["decision_id"],
    )
    digest = canonical_sha256(protocol)
    loader = lambda path=None: (protocol, digest)
    monkeypatch.setattr(blind_label_packets, "load_blind_label_protocol", loader)
    monkeypatch.setattr(blind_labels, "load_blind_label_protocol", loader)
    return dataset_directory, points, protocol, manifest, visible_path


def _submission(packet, escrow, protocol):
    annotations = []
    for item in packet["items"]:
        ref = item["evidence_catalog"][0]["evidence_ref"]
        label = {
            "context_regime": "UNCERTAIN",
            "higher_timeframe_bias": "UNCERTAIN",
            "location": "UNKNOWN",
            "signal_setup": "UNKNOWN",
            "signal_quality": "UNKNOWN",
            "supported_market_bias": "UNKNOWN",
            "wait_reference": "UNKNOWN",
            "target_structure": "UNKNOWN",
            "evidence_refs": [ref],
            "target_evidence_refs": [],
            "counter_evidence_refs": [],
            "rationale": "Fixture annotation cites a frozen bar and makes no use of outcomes.",
        }
        annotations.append({
            "packet_item_id": item["packet_item_id"],
            "labelled_at": "2026-10-10T00:00:00Z",
            "label": label,
        })
    return {
        "schema_version": blind_label_packets.SUBMISSION_SCHEMA_VERSION,
        "packet_id": packet["packet_id"],
        "packet_sha256": escrow["packet_sha256"],
        "rater_id": packet["rater_id"],
        "protocol_id": packet["protocol_id"],
        "protocol_sha256": packet["protocol_sha256"],
        "dataset_id": packet["dataset_id"],
        "dataset_manifest_sha256": packet["dataset_manifest_sha256"],
        "time_display_policy_id": packet["time_display_policy_id"],
        "annotations": annotations,
    }


def test_packet_hides_mapping_excludes_sealed_data_and_is_causal(
    frozen_visible_dataset,
):
    dataset_directory, points, _protocol, _manifest, visible_path = frozen_visible_dataset
    before = visible_path.read_bytes()
    packet, escrow = create_blind_annotation_packet(dataset_directory, rater_id="reviewer_a")

    assert len(packet["items"]) == 54
    assert packet["blinding"] == {
        "model_output_visible": False,
        "future_outcomes_visible": False,
        "experiment_arm_visible": False,
        "untouched_test_payload_visible": False,
        "partition_visible": False,
        "decision_id_visible": False,
    }
    assert {row["partition"] for row in escrow["crosswalk"]} == {"optimization", "validation"}
    public_json = json.dumps(packet, ensure_ascii=False)
    assert all(point["decision_id"] not in public_json for point in points)
    assert all(point["decision_time"] not in public_json for point in points)
    assert all(
        value not in public_json
        for point in points
        for rows in point["bars_by_timeframe"].values()
        for bar in rows
        for value in (bar["bar_start"], bar["bar_end"], bar["available_at"])
    )
    assert "optimization" not in public_json and "validation" not in public_json
    assert packet["time_display_policy_id"] == blind_label_packets.TIME_DISPLAY_POLICY_ID
    assert all(item["decision_time"] == "T0" for item in packet["items"])
    assert all(
        value == "T0" or value.startswith(("T-", "T+"))
        for item in packet["items"]
        for rows in item["bars_by_timeframe"].values()
        for bar in rows
        for value in (bar["bar_start"], bar["bar_end"], bar["available_at"])
    )
    normalized_times = {
        datetime.fromisoformat(point["decision_time"])
        .astimezone(UTC).isoformat().replace("+00:00", "Z")
        for point in points
    }
    assert {row["decision_time"] for row in escrow["crosswalk"]} == normalized_times
    assert all(set(item) == blind_label_packets._PUBLIC_ITEM_FIELDS for item in packet["items"])
    assert all("partition" not in item and "decision_id" not in item for item in packet["items"])
    assert all(item["provenance"]["availability_evidence_grade"] == ["NOT_VERIFIED_FIXTURE"]
               for item in packet["items"])
    assert not (dataset_directory / "untouched-test-sealed-manifest.json").exists()
    assert visible_path.read_bytes() == before


def test_static_reviewer_page_is_self_contained_and_network_disabled(frozen_visible_dataset):
    dataset_directory, _points, _protocol, _manifest, _visible = frozen_visible_dataset
    packet, _escrow = create_blind_annotation_packet(dataset_directory, rater_id="reviewer_a")
    html = render_blind_annotation_html(packet)

    assert "Content-Security-Policy" in html
    assert "connect-src 'none'" in html
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "WebSocket" not in html
    assert "<link" not in html
    assert "__V38_PACKET_BASE64__" not in html
    encoded = html.split('<script id="packet-data" type="application/octet-stream">', 1)[1].split("</script>", 1)[0]
    decoded_packet = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert decoded_packet["packet_sha256"] == packet["packet_sha256"]


def test_two_complete_submissions_rebind_and_validate_without_opening_sealed_file(
    frozen_visible_dataset,
):
    dataset_directory, _points, protocol, _manifest, _visible = frozen_visible_dataset
    packet_a, escrow_a = create_blind_annotation_packet(dataset_directory, rater_id="reviewer_a")
    packet_b, escrow_b = create_blind_annotation_packet(dataset_directory, rater_id="reviewer_b")
    result = import_blind_label_submissions(dataset_directory, [
        (escrow_a, _submission(packet_a, escrow_a, protocol)),
        (escrow_b, _submission(packet_b, escrow_b, protocol)),
    ])

    assert result["validation_status"] == "VALIDATED_MINIMUM_RATER_COVERAGE"
    assert result["rater_count"] == 2
    assert result["decision_count"] == 54
    assert result["annotation_count"] == 108
    assert {record["partition"] for record in result["annotations"]} == {"optimization", "validation"}
    assert not (dataset_directory / "untouched-test-sealed-manifest.json").exists()


def test_submission_requires_full_coverage_and_rejects_arm_or_outcome_injection(
    frozen_visible_dataset,
):
    dataset_directory, _points, protocol, _manifest, _visible = frozen_visible_dataset
    packets = [create_blind_annotation_packet(dataset_directory, rater_id=code)
               for code in ("reviewer_a", "reviewer_b")]
    pairs = [(escrow, _submission(packet, escrow, protocol)) for packet, escrow in packets]
    pairs[0][1]["annotations"].pop()
    with pytest.raises(BlindLabelPacketError, match="BLIND_SUBMISSION_COVERAGE_INCOMPLETE"):
        import_blind_label_submissions(dataset_directory, pairs)

    packets = [create_blind_annotation_packet(dataset_directory, rater_id=code)
               for code in ("reviewer_c", "reviewer_d")]
    pairs = [(escrow, _submission(packet, escrow, protocol)) for packet, escrow in packets]
    pairs[0][1]["annotations"][0]["label"]["future_outcome"] = "WIN"
    with pytest.raises(BlindLabelPacketError, match="BLIND_SUBMISSION_LABEL_FIELDS_INVALID"):
        import_blind_label_submissions(dataset_directory, pairs)


def test_submission_binds_to_packet_and_two_distinct_raters(frozen_visible_dataset):
    dataset_directory, _points, protocol, _manifest, _visible = frozen_visible_dataset
    packets = [create_blind_annotation_packet(dataset_directory, rater_id=code)
               for code in ("reviewer_a", "reviewer_b")]
    pairs = [(escrow, _submission(packet, escrow, protocol)) for packet, escrow in packets]
    pairs[0][1]["packet_sha256"] = "0" * 64
    with pytest.raises(BlindLabelPacketError, match="BLIND_SUBMISSION_PACKET_BINDING_INVALID"):
        import_blind_label_submissions(dataset_directory, pairs)

    packets = [create_blind_annotation_packet(dataset_directory, rater_id=code)
               for code in ("reviewer_c", "reviewer_c")]
    pairs = [(escrow, _submission(packet, escrow, protocol)) for packet, escrow in packets]
    with pytest.raises(BlindLabelPacketError, match="BLIND_SUBMISSION_RATER_OR_PACKET_DUPLICATE"):
        import_blind_label_submissions(dataset_directory, pairs)


def test_manifest_binding_and_exclusive_output_fail_closed(frozen_visible_dataset, tmp_path):
    dataset_directory, _points, _protocol, _manifest, visible_path = frozen_visible_dataset
    packet, _escrow = create_blind_annotation_packet(dataset_directory, rater_id="reviewer_a")
    destination = tmp_path / "pack.json"
    write_json_exclusive(destination, packet)
    with pytest.raises(BlindLabelPacketError, match="BLIND_PACKET_OUTPUT_EXISTS"):
        write_json_exclusive(destination, packet)

    visible_path.write_text(visible_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(BlindLabelPacketError, match="BLIND_PACKET_VISIBLE_INPUT_HASH_MISMATCH"):
        create_blind_annotation_packet(dataset_directory, rater_id="reviewer_b")
