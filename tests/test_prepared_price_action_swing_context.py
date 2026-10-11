"""Synthetic fixtures test activated swing facts; they are not market evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from scripts.prepare_price_action_swing_context_patch import FIELD, TARGET, ROOT, proposed_namespace, proposed_source, prepare
from tests.test_price_action_structure import _native_bars, _mirror_rows, _structure_fixture
from scripts.probe_prepared_price_action_swing_context import prepared_research_builder, probe, capacity_fixture_system
from core.trading.price_action_structure import _build_price_action_structure, build_price_action_structure
from core.replay.gemini_research import research_price_action
import pytest


def fixtures():
    rows = _native_bars(datetime(2026, 10, 3, 12, tzinfo=timezone.utc), count=60)
    rows[10]["high"], rows[25]["high"] = 110, 115
    rows[17]["low"], rows[34]["low"] = 90, 95
    rows[-1]["high"], rows[-1]["low"] = 500, 1
    return rows


def build(rows, at=None):
    return _build_price_action_structure(rows, "15m", at or rows[-1]["bar_end"])


def test_pairs_survive_compaction_without_unconfirmed_latest_extremes():
    result = build(fixtures())
    assert result["status"] == "READY"
    assert result[FIELD] == {"HIGH": 110, "LOW": 90}
    assert len(result["confirmed_swings"]) == 2
    assert len(json.dumps(result, separators=(",", ":"))) <= 1400


def test_pairs_are_symmetric_without_inventing_direction():
    mirrored = build(_mirror_rows(fixtures()))
    assert mirrored[FIELD] == {"HIGH": 110, "LOW": 90}
    assert "trend" not in mirrored[FIELD]


def test_unconfirmed_and_not_yet_available_pivots_excluded():
    rows = fixtures()
    at = rows[26]["bar_end"]  # second high at 25 needs right bars 26 and 27.
    prefix = build(rows, at)
    # Prefix is deliberately under the minimum 32 bars, and fails closed.
    assert prefix["status"] == "UNAVAILABLE"
    rows[42]["high"] = 120
    before = build(rows[:44], rows[43]["bar_end"])
    assert before[FIELD]["HIGH"] == 110
    after = build(rows[:45], rows[44]["bar_end"])
    assert after[FIELD]["HIGH"] == 115
    delayed = deepcopy(rows)
    delayed[44]["available_at"] = rows[50]["bar_end"]
    unavailable_confirmation = build(delayed[:45], rows[44]["bar_end"])
    assert unavailable_confirmation[FIELD]["HIGH"] == 110
    assert unavailable_confirmation["last_closed_at"] == rows[43]["bar_end"].replace("+00:00", "Z")


def test_short_lists_are_not_padded_or_promoted_to_active_setup():
    rows = _native_bars(datetime(2026, 10, 3, 12, tzinfo=timezone.utc), count=60)
    rows[10]["high"] = 110
    result = build(rows)
    assert result[FIELD] == {}
    assert result["bos"] is None


def test_existing_structure_states_prices_and_causal_times_are_unchanged():
    from core.trading.price_action_structure import build_price_action_structure
    rows = _structure_fixture(datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
    before, after = build_price_action_structure(rows, "15m", rows[-1]["bar_end"]), build(rows)
    assert after["status"] == "READY"
    for field in ("confirmed_swings", "bos", "sweep_reclaim", "breakout_retest", "prior_range", "as_of"):
        assert after[field] == before[field]


def test_preparation_never_writes_production_source(tmp_path):
    before = (ROOT / TARGET).read_bytes()
    with pytest.raises(ValueError, match="SOURCE_ANCHOR_CHANGED"):
        prepare(tmp_path)
    assert (ROOT / TARGET).read_bytes() == before


def test_prepared_research_does_not_relabel_test_gate_rows_as_binance_archive():
    rows = fixtures()
    result = research_price_action(rows, "15m", rows[-1]["bar_end"])
    assert result["status"] == "UNAVAILABLE"
    assert result["source_scope"] == "BINANCE_FROZEN_ARCHIVE_RESEARCH_ONLY_NOT_GATE_EVIDENCE"
    assert FIELD not in result


def test_probe_refuses_heldout_before_reading_any_history(tmp_path):
    from core.replay.ai_history import digest
    from core.replay.ai_template_runner import frozen_source_fingerprint
    plan = {"source_sha256": frozen_source_fingerprint(),
        "pilot_windows": [{"id": "validation-1", "partition": "validation"}]}
    (tmp_path / "research-plan.json").write_text(json.dumps({"plan": plan, "plan_sha256": digest(plan)}))
    with pytest.raises(ValueError, match="OPTIMIZATION_ONLY"):
        probe(tmp_path)


def test_capacity_fixture_uses_real_candidate_section_without_activating_or_changing_policy():
    from core.replay.ai_template_runner import frozen_templates, frozen_source_fingerprint
    from core.trading.autonomous_strategy import build_nofx_gate_system_prompt
    from core.trading.ai_session_coordinator import _compact_gate_system_prompt, _gate_repair_system_prompt
    before = frozen_source_fingerprint()
    strategy = frozen_templates(template_ids=("price_action_structure",))[0]
    original = _compact_gate_system_prompt(build_nofx_gate_system_prompt(strategy), strategy)
    fitted, metadata = capacity_fixture_system(original, 500)
    candidate = frozen_templates({"price_action_structure": "容" * 500}, ("price_action_structure",))[0]
    expected = frozen_templates(template_ids=("price_action_structure",))[0]
    expected["sections"]["custom_prompt"] = candidate["sections"]["custom_prompt"]
    assert fitted == _compact_gate_system_prompt(build_nofx_gate_system_prompt(expected), expected)
    assert "研究候选：" + "容" * 500 in fitted
    assert metadata["candidate_enabled"] is False
    assert metadata["scope"] == "CHINESE_TEXT_CAPACITY_FIXTURE_NOT_AI_AUTHORED_CANDIDATE"
    repair = _gate_repair_system_prompt(original, "repair contract only")
    repaired, _ = capacity_fixture_system(repair, 500)
    assert repaired == _gate_repair_system_prompt(fitted, "repair contract only")
    assert frozen_source_fingerprint() == before


def test_activated_seed_and_future_candidate_survive_real_production_prompt_without_truncation():
    from core.trading.ai_strategy_book import _PA_RESEARCH_SEED_INSTRUCTION
    from core.replay.ai_template_runner import frozen_templates
    from core.trading.autonomous_strategy import build_nofx_gate_system_prompt
    from core.trading.ai_session_coordinator import _compact_gate_system_prompt
    strategy = frozen_templates({'price_action_structure': '容' * 500}, ('price_action_structure',))[0]
    system = _compact_gate_system_prompt(build_nofx_gate_system_prompt(strategy), strategy)
    # A candidate-bearing replay template must not also receive the seed that
    # was used to create the experiment; this node name is frozen by baseline.
    assert _PA_RESEARCH_SEED_INSTRUCTION not in system
    assert '研究候选：' + '容' * 500 in system
    assert 'confirmed_at=max' in system
    assert strategy['execution']['fixed_notional_usdt'] == 2000
    assert strategy['execution']['leverage_mode'] == 'VENUE_LIMIT'


def test_capacity_fixture_refuses_unknown_duplicate_or_invalid_input_instead_of_truncating():
    from core.replay.ai_template_runner import frozen_templates
    base = frozen_templates(template_ids=("price_action_structure",))[0]["sections"]["custom_prompt"]
    for system in ("unknown prompt", base + base):
        with pytest.raises(ValueError, match="BASE_INSTRUCTION_NOT_UNIQUE"):
            capacity_fixture_system(system, 500)
    for count in (-1, 501, True, 2.5):
        with pytest.raises(ValueError, match="CHAR_LIMIT_INVALID"):
            capacity_fixture_system(base, count)
    assert capacity_fixture_system("unaltered", 0) == ("unaltered", None)


def test_prepared_application_budget_is_bounded_and_refuses_invalid_before_archive_access(tmp_path):
    for count in (-1, True, 12000, 65536, "12288"):
        with pytest.raises(ValueError, match="PREPARED_INPUT_BUDGET_INVALID"):
            probe(tmp_path, 500, count)
    for count in (0, 8192, 12288, 16384, 32768):
        with pytest.raises(FileNotFoundError):
            probe(tmp_path, 500, count)


def test_production_technical_context_and_compact_view_keep_prior_confirmed_prices():
    from types import SimpleNamespace
    from core.trading.autonomous_strategy import technical_context, compact_technical
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    frames = {}
    for timeframe in ("15m", "1h"):
        rows = _native_bars(now, timeframe, count=60)
        rows[10]["high"], rows[25]["high"] = 110, 115
        rows[17]["low"], rows[34]["low"] = 90, 95
        frames[timeframe] = rows
    store = SimpleNamespace(latest_bars=lambda symbol, frame, **kwargs: frames[frame],
        build_price_action_evidence=build_price_action_structure)
    technical = technical_context(store, ("ETHUSDT",), now,
        timeframes=("15m", "1h"), include_price_action=True)
    compact = compact_technical(technical, signal_timeframe="15m")
    for timeframe in ("15m", "1h"):
        before = technical["ETHUSDT"]["timeframes"][timeframe]["price_action"]
        after = compact["ETHUSDT"]["timeframes"][timeframe]["price_action"]
        assert before[FIELD] == after[FIELD] == {"HIGH": 110, "LOW": 90}
        assert before["confirmed_swings"] == after["confirmed_swings"]


def test_three_symbol_encoding_preserves_prior_swings_and_execution_facts_or_explicitly_refuses():
    from core.trading.ai_session_coordinator import _fit_prompt_payload
    from tests.test_three_symbol_prompt_projection import _three_pa_payload, _comparison_facts
    from tests.prompt_evidence_helpers import decode_price_action
    payload = _three_pa_payload()
    for index, symbol in enumerate(payload["allowed_instruments"]):
        for frame in ("15m", "1h"):
            payload["technical_context"][symbol]["timeframes"][frame]["price_action"][FIELD] = {"HIGH": 61000 + index, "LOW": 60500 + index}
    original = deepcopy(payload)
    fitted, metadata = _fit_prompt_payload(payload, "系统规则" * 1000, 8192, reserve=1024,
        signal_timeframe="15m", token_counter=lambda text: (len(text)+1)//2,
        required_symbols=tuple(payload["allowed_instruments"]), allow_symbol_deferral=False)
    assert "price_action_encoding" in fitted["technical_context"]
    assert metadata["estimated_input_tokens"] + 1280 <= 8192
    assert fitted["account_truth"] == original["account_truth"]
    assert fitted["allowed_instruments"] == original["allowed_instruments"]
    for symbol in fitted["allowed_instruments"]:
        for field in ("price", "bid", "ask", "fee_rate", "slippage", "contract_rules", "data_as_of"):
            assert fitted["market_snapshots"][symbol].get(field) == original["market_snapshots"][symbol].get(field)
    for symbol in fitted["allowed_instruments"]:
        for frame in ("15m", "1h"):
            actual = decode_price_action(fitted["technical_context"], fitted["technical_context"][symbol]["timeframes"][frame]["price_action"])
            before = original["technical_context"][symbol]["timeframes"][frame]["price_action"]
            assert actual[FIELD] == before[FIELD]
            assert _comparison_facts(actual) == _comparison_facts(before)
    assert original == payload
    with pytest.raises(ValueError, match="AI_INPUT_BUDGET_EXCEEDED"):
        _fit_prompt_payload(payload, "system", 1000, reserve=1024,
            required_symbols=tuple(payload["allowed_instruments"]), allow_symbol_deferral=False)


def test_real_coordinator_repair_and_simulated_execution_keep_prepared_swing_facts(tmp_path, monkeypatch):
    """Fake provider and synthetic bars; not real Gemini or Gate acceptance."""
    from core.model_client import model_client
    from core.replay.ai_history import ReplayHistory, manifest_hash
    from core.replay import ai_template_runner as runner
    from tests.test_ai_template_runner import history, rows, FixtureProvider
    from tests.prompt_evidence_helpers import decode_price_action
    source = deepcopy(history(5).payload)
    for frame in ("15m", "1h"):
        bars = sorted((b for b in source["bars"] if b["timeframe"] == frame), key=lambda b: b["bar_end"])
        bars[150]["high"], bars[180]["high"] = 110, 115
        bars[160]["low"], bars[200]["low"] = 90, 95
    source["manifest_sha256"] = manifest_hash(source)
    before_files = runner.frozen_source_fingerprint()
    prepared = build_price_action_structure
    # This monkeypatch is scoped to a separate pytest process, never the live runner.
    monkeypatch.setattr(runner._ReplayStore, "build_price_action_evidence", lambda self, bars, frame, at: prepared(bars, frame, at))
    monkeypatch.setattr(model_client, "count_tokens", lambda text, **kwargs: max(1, len(text)//3))

    class Provider(FixtureProvider):
        def generate_json(self, messages, **options):
            wrapper = json.loads(messages[1]["content"])
            repair = "previous_decision" in wrapper
            inputs = wrapper["inputs"] if repair else wrapper
            self.payloads.append(deepcopy(inputs))
            for frame in ("15m", "1h"):
                actual = decode_price_action(inputs["technical_context"], inputs["technical_context"]["ETHUSDT"]["timeframes"][frame]["price_action"])
                assert actual[FIELD] == {"HIGH": 110, "LOW": 90}
                assert {s["side"]: s["price"] for s in actual["confirmed_swings"]} == {"HIGH": 115, "LOW": 95}
            decision = {"action": "OPEN_LONG", "instrument_id": "ETHUSDT", "reason": "synthetic prepared context repair",
                "confidence": 80, "entry_price": 100, "stop_price": 90, "take_profit": 200,
                "evidence_refs": [r for r in inputs["evidence_refs"] if r.startswith("market_snapshot:ETHUSDT:")]}
            if repair:
                assert inputs["account_truth"] == self.payloads[0]["account_truth"]
                previous = wrapper["previous_decision"]
                for key in ("action", "instrument_id", "entry_price", "stop_price", "take_profit"):
                    assert decision[key] == previous[key]
                decision.update(position_size_usdt=2000, requested_leverage=40, order_preference="MARKET")
            raw = json.dumps(decision)
            return decision, raw, {"model_id": self.model_id, "model_version": self.model_id,
                "actual_model_id": self.model_id, "model_identity_source": "completion_response",
                "verified_manifest_model_id": self.model_id}

    provider = Provider()
    database = tmp_path / "prepared-synthetic.sqlite3"
    result = runner.run_ai_template_replay(ReplayHistory(source), db_path=database, model_provider=provider,
        priority_guard=lambda: None, max_decisions=1, template_ids=("price_action_structure",),
        initial_equity=200_000.0)
    assert result["errors"] == []
    assert result["decision_source"] == "TEST_PROVIDER" and result["comparison_eligible"] is False
    assert len(provider.payloads) == 2
    record = rows(database)[0]
    assert record["status"] == "COMPLETED"
    decision, receipt = json.loads(record["decision_json"]), json.loads(record["result_json"])
    assert decision["position_size_usdt"] == 2000 and decision["requested_leverage"] == 40
    assert (decision["entry_price"], decision["stop_price"], decision["take_profit"]) == (100, 90, 200)
    assert receipt["status"] == "SUBMITTED" and receipt["events"][0]["status"] == "ACCEPTED"
    assert receipt["private_exchange_calls"] == 0
    assert runner.frozen_source_fingerprint() == before_files
