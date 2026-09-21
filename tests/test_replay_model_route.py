from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar

import pytest

from core.ai import MockLLMProvider, OllamaProvider
from core.model_routing import DEFAULT_MODEL
from core.replay.runner import MOCK_MODEL_ID, ReplayConfig, run_replay


def test_replay_rejects_non_bonsai_model_before_creating_database(tmp_path) -> None:
    database = tmp_path / "rejected-replay.sqlite3"
    config = ReplayConfig(
        symbols=("BTCUSDT",),
        timeframes=("15m",),
        samples=1,
        model_id="qwen3.5:9b",
        db_path=str(database),
        manifest_path=str(tmp_path / "manifest.json"),
    )

    with pytest.raises(ValueError, match="pinned to the manifest-verified Bonsai"):
        run_replay(config)
    assert not database.exists()


def test_replay_default_is_the_single_bonsai_model() -> None:
    config = ReplayConfig(symbols=("BTCUSDT",), timeframes=("15m",), samples=1)
    assert config.model_id == DEFAULT_MODEL
    assert config.model_mode == "real"


def test_replay_rejects_mock_provider_under_bonsai_identity_before_database_creation(tmp_path) -> None:
    database = tmp_path / "mock-as-bonsai.sqlite3"
    config = ReplayConfig(
        symbols=("BTCUSDT",),
        timeframes=("15m",),
        samples=1,
        model_id=DEFAULT_MODEL,
        db_path=str(database),
        manifest_path=str(tmp_path / "manifest.json"),
    )

    with pytest.raises(ValueError, match="exact production OllamaProvider"):
        run_replay(config, llm_provider=MockLLMProvider())
    assert not database.exists()


def test_replay_rejects_provider_that_only_claims_bonsai_identity(tmp_path) -> None:
    class ForgedBonsaiProvider:
        provider_name = "bonsai_llama_server"
        model_id = DEFAULT_MODEL
        model_name = DEFAULT_MODEL

    database = tmp_path / "forged-bonsai.sqlite3"
    config = ReplayConfig(
        symbols=("BTCUSDT",),
        timeframes=("15m",),
        samples=1,
        db_path=str(database),
        manifest_path=str(tmp_path / "manifest.json"),
    )
    with pytest.raises(ValueError, match="exact production OllamaProvider"):
        run_replay(config, llm_provider=ForgedBonsaiProvider())
    assert not database.exists()


def test_replay_rejects_exact_provider_without_verified_bonsai_receipt(tmp_path, monkeypatch) -> None:
    provider = OllamaProvider()
    monkeypatch.setattr(
        provider,
        "health",
        lambda: {
            "available": True,
            "model_available": True,
            "model_id": DEFAULT_MODEL,
            "model_identity_source": "verified_manifest",
            "actual_model_id": "Other-Bonsai-2-27B-PTQ1_0.gguf",
        },
    )
    database = tmp_path / "unverified-bonsai.sqlite3"
    config = ReplayConfig(
        symbols=("BTCUSDT",),
        timeframes=("15m",),
        samples=1,
        db_path=str(database),
        manifest_path=str(tmp_path / "manifest.json"),
    )
    with pytest.raises(ValueError, match="verified Bonsai manifest receipt"):
        run_replay(config, llm_provider=provider)
    assert not database.exists()


def test_mock_provider_has_an_unambiguous_non_bonsai_model_identity() -> None:
    provider = MockLLMProvider()
    assert provider.provider_name == "mock_llm"
    assert provider.model_id == MOCK_MODEL_ID == "mock-llm"
    with pytest.raises(TypeError):
        MockLLMProvider(model_id=DEFAULT_MODEL)


def test_mock_replay_never_settles_or_saves_model_calibration(tmp_path, monkeypatch) -> None:
    import core.replay.runner as replay

    @dataclass(frozen=True)
    class FakeAction:
        value: str = "LONG"

    @dataclass(frozen=True)
    class FakeSignal:
        action: FakeAction
        parse_status: str
        prediction_id: str | None = None
        model_id: str = MOCK_MODEL_ID

    @dataclass(frozen=True)
    class FakeResult:
        context: object
        signal: FakeSignal
        model_status: dict

    class FakeStore:
        instances: ClassVar[list[FakeStore]] = []

        def __init__(self, _path):
            self.__class__.instances.append(self)
            self.samples = []
            self.persisted = []
            self.outcomes = []
            self.performance = []
            self.replay_config = None

        def initialize(self): pass
        def find_resumable_replay_run(self, _hash): return None
        def create_replay_run(self, **kwargs): self.replay_config = kwargs
        def update_replay_run(self, *_args, **_kwargs): pass
        def get_replay_sample(self, *_args): return None
        def save_replay_sample(self, **kwargs): self.samples.append(kwargs)
        def list_replay_samples(self, _run_id): return self.samples
        def list_prediction_records(self, **_kwargs): return []
        def save_outcome(self, outcome): self.outcomes.append(outcome)
        def save_performance_snapshot(self, value): self.performance.append(value)
        def save_calibration_result(self, _value):
            raise AssertionError("mock replay must not persist calibration")
        def list_replay_model_latencies(self, _run_id): return []

    class FakeAnalysisService:
        def __init__(self, *, store=None, **_kwargs):
            self.store = store

        def analyze(self, _instrument, *, analysis_time, **_kwargs):
            bar = SimpleNamespace(timestamp=analysis_time)
            return FakeResult(
                context=SimpleNamespace(bars=[bar]),
                signal=FakeSignal(FakeAction(), "valid"),
                model_status={"model_id": MOCK_MODEL_ID, "available": True, "latency_ms": 1.0},
            )

        def persist(self, result):
            self.store.persisted.append(result.signal)

    point = datetime(2026, 1, 1, tzinfo=UTC)
    history = SimpleNamespace(provider="fixture", bars=())
    monkeypatch.setattr(replay, "SQLiteStore", FakeStore)
    monkeypatch.setattr(replay, "AnalysisService", FakeAnalysisService)
    monkeypatch.setattr(
        replay,
        "_sample_plan",
        lambda _config: (
            [{"symbol": "BTCUSDT", "timeframe": "15m", "as_of": point, "history": history}],
            {"routes": [], "sampling_policy": {"news_history_available": False}},
        ),
    )

    result = run_replay(
        ReplayConfig(
            symbols=("BTCUSDT",),
            timeframes=("15m",),
            samples=1,
            model_id=MOCK_MODEL_ID,
            model_mode="mock",
            db_path=str(tmp_path / "mock-replay.sqlite3"),
            output_path=str(tmp_path / "result.json"),
            manifest_path=str(tmp_path / "manifest.json"),
        ),
        llm_provider=MockLLMProvider(),
    )

    store = FakeStore.instances[-1]
    # The public result and durable replay metadata carry an explicit mock identity.
    assert result["model_id"] == MOCK_MODEL_ID
    assert result["model_mode"] == "mock"
    assert result["model_provenance"] == "mock_simulation"
    assert result["formal_acceptance_eligible"] is False
    assert result["source_type"] == "mock_replay"
    assert result["calibration"]["status"] == "DISABLED_FOR_MOCK_MODE"
    assert store.replay_config["model_id"] == MOCK_MODEL_ID
    assert store.replay_config["config"]["model_mode"] == "mock"
    assert store.persisted and store.persisted[0].model_id == MOCK_MODEL_ID
    assert store.outcomes == []
    assert store.performance == []
