from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import scripts.run_phase2_demo as demo
from core.ai import MockLLMProvider


def test_mock_demo_is_marked_demo_only_and_passes_demo_source_type(monkeypatch, tmp_path, capsys) -> None:
    observed = {}

    class FakeService:
        def __init__(self, *, llm_provider, **_kwargs):
            observed["llm_provider"] = llm_provider

        def analyze(self, _instrument, **kwargs):
            observed["source_type"] = kwargs["source_type"]
            point = datetime(2026, 1, 1, tzinfo=timezone.utc)
            signal = SimpleNamespace(
                action=SimpleNamespace(value="WAIT"),
                raw_confidence=0.3,
                prediction_id="demo-prediction",
                entry_low=None,
                entry_high=None,
                stop=None,
                tp1=None,
                tp2=None,
                signal_valid_until=point,
                max_hold_until=point,
            )
            return SimpleNamespace(
                bundle=SimpleNamespace(snapshot=SimpleNamespace(provider="fixture", stale=True), quote=SimpleNamespace(price=100.0)),
                data_as_of=point,
                response_time=point,
                context=SimpleNamespace(quant=SimpleNamespace(to_dict=lambda: {})),
                news=SimpleNamespace(events=(), available=False),
                model_status={"model_id": MockLLMProvider().model_id},
                signal=signal,
            )

    class FakeStore:
        def __init__(self, _path): pass
        def counts(self): return {"predictions": 1}

    monkeypatch.setattr(demo, "AnalysisService", FakeService)
    monkeypatch.setattr(demo, "SQLiteStore", FakeStore)

    assert demo.main([
        "--mode", "fixture",
        "--llm", "mock",
        "--news", "fixture",
        "--symbols", "BTCUSDT",
        "--db", str(tmp_path / "demo.sqlite3"),
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert observed["source_type"] == "demo"
    assert observed["llm_provider"].model_id == "mock-llm"
    assert payload["source_type"] == "demo"
    assert payload["model_provenance"] == "mock_simulation"
    assert payload["formal_acceptance_eligible"] is False
    assert payload["results"][0]["model"]["model_id"] == "mock-llm"


@pytest.mark.parametrize("llm", ["mock", "ollama", "off"])
@pytest.mark.parametrize("flag", ["--follow", "--settle"])
def test_demo_never_creates_paper_trades_or_outcomes(tmp_path, llm, flag) -> None:
    database = tmp_path / f"blocked-demo-{llm}-{flag[2:]}.sqlite3"
    with pytest.raises(SystemExit) as exc:
        demo.main([
            "--llm", llm,
            "--symbols", "BTCUSDT",
            "--db", str(database),
            flag,
        ])

    assert exc.value.code == 2
    assert not database.exists()
