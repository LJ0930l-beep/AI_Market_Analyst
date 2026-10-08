"""Offline in-memory preparation checks; no source activation or trading proof."""
from copy import deepcopy
import json

import pytest

from core.replay.ai_template_runner import frozen_templates
from scripts.prepare_pa_research_isolation import PA, ROOT, proposed_sources, prepared_context, prepare


@pytest.fixture
def prepared():
    return prepared_context(proposed_sources())


def test_production_has_no_experimental_seed(prepared):
    book, _ = prepared
    pa = next(item for item in book["TEMPLATES"] if item["id"] == PA)
    assert book["_PA_RESEARCH_SEED_INSTRUCTION"] not in json.dumps(pa, ensure_ascii=False)
    assert "研究优化种子：" not in pa["sections"]["entry_standards"]


def test_optimization_config_and_hash_exactly_preserved(prepared):
    _, freeze = prepared
    assert freeze(template_ids=[PA]) == frozen_templates(template_ids=[PA])


def test_all_legacy_templates_preserved_in_research(prepared):
    _, freeze = prepared
    assert freeze() == frozen_templates()


@pytest.mark.parametrize("length", [1, 246, 500])
def test_candidate_replaces_seed_and_preserves_constraints(prepared, length):
    book, freeze = prepared
    candidate = "验" * length
    actual = freeze({PA: candidate}, [PA])[0]
    original = frozen_templates(template_ids=[PA])[0]
    assert book["_PA_RESEARCH_SEED_INSTRUCTION"] not in json.dumps(actual, ensure_ascii=False)
    assert actual["sections"]["custom_prompt"].endswith("\n研究候选：" + candidate)
    for field in ("profile", "execution", "template_id", "style", "revision"):
        assert actual[field] == original[field]
    assert actual["config_sha256"] != original["config_sha256"]


def test_template_base_not_mutated(prepared):
    book, freeze = prepared
    original = deepcopy(book["TEMPLATES"])
    freeze(template_ids=[PA])
    freeze({PA: "独立候选"}, [PA])
    assert book["TEMPLATES"] == original


@pytest.mark.parametrize("candidate", [{}, {PA: ""}, {PA: "验" * 501}, {PA: 2}])
def test_invalid_candidates_remain_rejected(prepared, candidate):
    _, freeze = prepared
    with pytest.raises(ValueError, match="RESEARCH_CANDIDATE_SET_INVALID"):
        freeze(candidate, [PA])


def test_live_files_not_changed():
    sources = proposed_sources()
    before = {path: (ROOT / path).read_bytes() for path in sources}
    prepared_context(sources)
    assert all((ROOT / path).read_bytes() == value for path, value in before.items())


def test_receipt_and_patch_are_preparation_only(tmp_path):
    (tmp_path / "research-plan.json").write_text(json.dumps(
        {"plan_sha256": "a" * 64, "plan": {"research_only": True}}), encoding="utf-8")
    result = prepare(tmp_path)
    assert result["status"] == "PREPARED_NOT_APPLIED"
    assert result["plan_sha256"] == "a" * 64
    assert result["model_calls"] == result["exchange_calls"] == 0
    assert result["existing_trade_constraints_changed"] is False
    assert len(result["source_before_sha256"]) == 2
    assert (tmp_path / "next-freeze-production-research-isolation.patch").is_file()
