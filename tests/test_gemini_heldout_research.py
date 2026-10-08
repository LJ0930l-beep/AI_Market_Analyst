"""Pure fixtures for research isolation and candidate freeze contracts."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from core.replay.ai_template_runner import frozen_templates, TEMPLATE_IDS
from core.replay.ai_history import utc
from scripts.run_gemini_heldout_research import heldout_plan, candidate_instructions
from scripts.run_gemini_heldout_research import require_matching_input_budget


def test_heldout_capacity_must_match_registered_optimization_without_changing_source():
    require_matching_input_budget({'application_input_budget':12288}, {'application_input_budget':12288})
    for value in (None, 8192, True, '12288'):
        with pytest.raises(ValueError, match='APPLICATION_INPUT_BUDGET_MUST_MATCH'):
            require_matching_input_budget({'application_input_budget':value}, {'application_input_budget':12288})


def proposals():
    return {template: "仅在可核验结构成立时交易，保持固定金额和保护。" for template in TEMPLATE_IDS}


def test_candidate_text_changes_hash_without_changing_execution_or_global_defaults():
    original = frozen_templates()
    revised = frozen_templates(proposals())
    assert frozen_templates() == original
    for base, candidate in zip(original, revised):
        assert candidate["config_sha256"] != base["config_sha256"]
        assert candidate["profile"] == base["profile"]
        assert candidate["execution"] == base["execution"]
        assert candidate["execution"]["fixed_notional_usdt"] == 2000
        assert candidate["sections"]["custom_prompt"].startswith(base["sections"]["custom_prompt"])


@pytest.mark.parametrize("invalid", [None, {}, {"aggressive_impulse": "x"}, {key: "x" * 501 for key in TEMPLATE_IDS}])
def test_non_complete_candidates_are_rejected(invalid):
    if invalid is None:
        invalid = {key: None for key in TEMPLATE_IDS}
    with pytest.raises(ValueError, match="CANDIDATE_SET"):
        frozen_templates(invalid)


@pytest.mark.parametrize("phase", ["validation", "untouched_test"])
def test_preregistered_windows_never_overlap_optimization_or_other_partition(phase):
    base = {"partitions": [
        {"id": "optimization", "start": "2025-10-01T00:00:00+00:00", "end": "2026-04-01T00:00:00+00:00"},
        {"id": "validation", "start": "2026-04-01T00:00:00+00:00", "end": "2026-07-01T00:00:00+00:00"},
        {"id": "untouched_test", "start": "2026-07-01T00:00:00+00:00", "end": "2026-10-01T00:00:00+00:00"}],
        "pilot_windows": [], "templates": frozen_templates()}
    old = deepcopy(base)
    plan = heldout_plan(base, proposals(), phase, "optimization-source")
    assert base == old
    partition = next(row for row in base["partitions"] if row["id"] == phase)
    assert len(plan["pilot_windows"]) == 3
    assert plan["expected_pilot_decisions"] == 4032
    for window in plan["pilot_windows"]:
        assert window["partition"] == phase
        assert utc(partition["start"]) <= utc(window["start"]) < utc(window["end"]) <= utc(partition["end"])
        assert utc(window["end"]) - utc(window["start"]) == timedelta(hours=48)
    with pytest.raises(ValueError, match="HELDOUT_PARTITION"):
        heldout_plan(base, proposals(), "optimization", "x")
    with pytest.raises(ValueError, match="PREREGISTERED_PROTOCOL"):
        heldout_plan({**base, "heldout_protocol": {"window_hours": 12,
                     "calendar_offsets_days": [14, 44, 74]}}, proposals(), phase, "x")


def test_candidate_request_must_bind_to_optimization_plan_before_receipt_is_used():
    artifact = {"plan_sha256": "source", "requires_validation": True,
        "request_messages": [{"role": "system", "content": "fixture"},
            {"role": "user", "content": json.dumps({"plan_sha256": "other"})}]}
    with pytest.raises(ValueError, match="REQUEST_BINDING"):
        candidate_instructions(artifact, "source")
