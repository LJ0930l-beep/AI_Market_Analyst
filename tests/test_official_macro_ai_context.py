"""Offline acceptance of official releases reaching the exact AI projection."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from core.trading.ai_session_coordinator import (
    AISessionCoordinator, _compact_news_revision, _fit_prompt_payload,
    _select_prompt_news, _estimate_tokens, _validated_official_macro_news,
)


def official_revision():
    return {
        "revision_id": "official_macro_fixture", "scope": "MARKET_WIDE",
        "symbol": None, "title": "官方宏观公布值", "summary": "仅是事实；多空含义由模型判断。",
        "source": "BLS", "impact": "UNKNOWN",
        "published_at": "2026-10-02T12:30:00+00:00",
        "known_at": "2026-10-02T23:20:00+00:00",
        "macro_releases": [{"title": "Non-Farm Employment Change", "actual": "29K",
            "previous": "162K", "forecast": "89K", "reference_period": "2026-09",
            "unit": "thousand_persons", "provider": "BLS",
            "source_url": "https://api.bls.gov/publicAPI/v1/timeseries/data/",
            "published_at": "2026-10-02T12:30:00+00:00",
            "available_at": "2026-10-02T23:20:00+00:00"}],
    }


def test_read_only_coordinator_includes_official_macro_without_rss_api(monkeypatch):
    import core.macro_calendar as calendar
    revision = official_revision()
    calls = []
    def reader(store, *, now):
        calls.append(now)
        return [deepcopy(revision)]
    monkeypatch.setattr(calendar, "official_macro_news", reader)
    coordinator = object.__new__(AISessionCoordinator)
    coordinator.store = object()  # No network, trading gateway or model.
    now = datetime(2026, 10, 2, 23, 21, tzinfo=timezone.utc)
    assert coordinator._news_revisions(("BTCUSDT",), now=now) == [revision]
    assert calls == [now]


def test_selected_official_release_values_survive_prose_compaction():
    revision = official_revision()
    original = deepcopy(revision)
    revision["summary"] *= 100
    compact = _compact_news_revision(revision)
    assert compact["macro_releases"] == original["macro_releases"]
    assert compact["known_at"] == original["known_at"]
    selected = _select_prompt_news([revision], ("BTCUSDT",))
    assert selected[0]["macro_releases"] == original["macro_releases"]
    assert selected[0]["impact"] == "UNKNOWN"


def test_official_macro_cannot_be_crowded_out_by_symbol_headlines():
    revision = official_revision()
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BCHUSDT")
    headlines = [{"revision_id": f"symbol_{symbol}", "scope": "SYMBOL", "symbol": symbol,
        "title": revision["title"] if index == 0 else f"{symbol} direct headline"}
        for index, symbol in enumerate(symbols)]
    selected = _select_prompt_news([*headlines, revision], symbols)
    assert len(selected) == 4
    assert selected[0]["macro_releases"] == revision["macro_releases"]


def test_ai_seam_rejects_future_or_ambiguously_timed_actuals():
    now = datetime(2026, 10, 2, 23, 21, tzinfo=timezone.utc)
    revision = official_revision()
    original = deepcopy(revision)
    future = deepcopy(revision)
    future["macro_releases"][0]["available_at"] = "2026-10-03T00:00:00Z"
    assert _validated_official_macro_news([future], now) == []
    ambiguous = deepcopy(revision)
    ambiguous["macro_releases"][0]["available_at"] = "2026-10-02T23:20:00"
    assert _validated_official_macro_news([ambiguous], now) == []
    good = _validated_official_macro_news([revision], now)
    assert good == [revision]
    assert revision == original
    conflict = deepcopy(revision)
    conflict["macro_releases"][0]["actual_available_at"] = "2026-10-03T00:00:00Z"
    assert _validated_official_macro_news([conflict], now) == []
    mixed = deepcopy(revision)
    mixed["macro_releases"].append(conflict["macro_releases"][0])
    assert _validated_official_macro_news([mixed], now)[0]["macro_releases"] == revision["macro_releases"]
    future_known = deepcopy(revision)
    future_known["macro_releases"][0]["known_at"] = "2026-10-03T00:00:00Z"
    assert _validated_official_macro_news([future_known], now) == []


def test_official_actuals_survive_real_8k_budget_projection():
    from tests.test_model_context_budget import (
        _production_sized_prompt_payload, _add_production_named_technical_evidence,
    )
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    # Use the same heavy account/news/technical fixture as production budget
    # regressions; this is synthetic input, not a model or real-order claim.
    revision = official_revision()
    payload["news_revisions"] = [revision, *payload["news_revisions"]]
    original = deepcopy(payload)
    system = "系统规则" * 507 + "。" * 3
    fitted, report = _fit_prompt_payload(payload, system, context_length=8192,
        reserve=1024, signal_timeframe="5m")
    observed = [r for r in fitted["news_revisions"] if r.get("macro_releases")]
    assert len(observed) == 1
    assert observed[0]["macro_releases"] == revision["macro_releases"]
    assert observed[0]["known_at"] == revision["known_at"]
    encoded = json.dumps(fitted, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert _estimate_tokens(system) + _estimate_tokens(encoded) + 1024 + 256 <= 8192
    assert report["steps"]
    assert payload == original


def test_captured_price_structure_and_full_macro_contract_roundtrip_in_8k():
    """Offline captured shape, not a claim of fresh production market data."""
    from pathlib import Path
    from tests.test_model_context_budget import (
        _production_sized_prompt_payload, _add_production_named_technical_evidence,
    )
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    frames = json.loads((Path(__file__).parent / "fixtures" / "price_action_capture_shape.json").read_text(encoding="utf-8"))["timeframes"]
    for symbol in payload["allowed_instruments"]:
        for timeframe in ("15m", "1h"):
            payload["technical_context"][symbol]["timeframes"][timeframe]["price_action"] = deepcopy(frames[timeframe])
    revision = official_revision()
    revision["macro_releases"] = [deepcopy(revision["macro_releases"][0]) for _ in range(4)]
    for fact, title, actual in zip(revision["macro_releases"],
            ("Non-Farm Employment Change", "Unemployment Rate", "Average Hourly Earnings m/m", "CPI Flash Estimate y/y"),
            ("29K", "4.2%", "0.1%", "3.8%")):
        fact.update(title=title, actual=actual, event_id=title, currency="USD",
            actual_value=actual.rstrip("K%"), basis="seasonally_adjusted",
            actual_status="VERIFIED", actual_available_at=fact["available_at"], known_at=fact["available_at"],
            schedule_source_url="https://www.forexfactory.com/calendar?week=this",
            published_at_source="scheduled_release_time", actual_method="DERIVED_FROM_OFFICIAL_SERIES", actual_is_estimate=False)
    revision["macro_releases"][-1].update(currency="EUR", provider="Eurostat",
        source_url="https://ec.europa.eu/eurostat/en/web/products-euro-indicators/w/2-02102026-ap",
        actual_method="OFFICIAL_REPORTED", actual_is_estimate=True, published_at_source="official_release_page")
    payload["news_revisions"] = [revision, *payload["news_revisions"]]
    original = deepcopy(payload)
    fitted, report = _fit_prompt_payload(payload, "系统规则" * 620,
        context_length=8192, reserve=1024, signal_timeframe="15m")
    assert report["estimated_input_tokens"] + 1024 + 256 <= 8192
    assert "compact_price_action_evidence_losslessly" in report["steps"]
    encoding = fitted["technical_context"]["price_action_encoding"]

    def decode_pa(value):
        if isinstance(value, dict):
            return {encoding["keys"].get(key, key): decode_pa(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decode_pa(item) for item in value]
        if isinstance(value, str) and value.startswith("@") and value[1:].isdigit():
            return encoding["times"][int(value[1:])]
        return value

    for symbol in fitted["allowed_instruments"]:
        for timeframe in ("15m", "1h"):
            projected_pa = fitted["technical_context"][symbol]["timeframes"][timeframe]["price_action"]
            assert {**encoding["defaults"], **decode_pa(projected_pa)} == frames[timeframe]
    projected = next(row for row in fitted["news_revisions"] if row.get("macro_releases"))
    macro_encoding = projected.get("macro_release_encoding")
    if macro_encoding:
        restored = []
        for index, row in enumerate(projected["macro_releases"]):
            missing = macro_encoding["missing_columns"][index]
            restored.append({key: macro_encoding["strings"][value["s"]] if isinstance(value, dict) else value
                for column, (key, value) in enumerate(zip(macro_encoding["columns"], row)) if column not in missing})
    else:
        restored = projected["macro_releases"]
    assert len(restored) == 4
    for fact, original_fact in zip(restored, revision["macro_releases"]):
        for key in ("actual", "reference_period", "provider", "source_url", "unit", "published_at", "available_at", "actual_method", "actual_is_estimate"):
            assert fact[key] == original_fact[key]
    assert payload == original
    # A second budget check must retain table metadata and remain reversible.
    fitted_again, _ = _fit_prompt_payload(fitted, "系统规则" * 620,
        context_length=8192, reserve=1024, signal_timeframe="15m")
    assert fitted_again == fitted


def test_price_structure_and_four_actuals_share_the_fixed_model_window():
    from tests.test_model_context_budget import (
        _production_sized_prompt_payload, _add_production_named_technical_evidence,
    )
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    for symbol in payload["allowed_instruments"]:
        for frame in payload["technical_context"][symbol]["timeframes"].values():
            frame["price_action"] = {"status": "READY", "as_of": "2026-10-02T23:21:00Z",
                "source": "gate_native_rest:last", "closed_bar_count": 240,
                "confirmed_swings": [{"side": "HIGH", "price": 61000,
                    "pivot_at": "2026-10-02T22:00:00Z", "confirmed_at": "2026-10-02T22:30:00Z"}],
                "prior_range": {"high": 61000, "low": 60500, "lookback_bars": 20},
                "bos": {"side": "LONG", "level": 61000, "bar_at": "2026-10-02T23:00:00Z",
                    "confirmed_at": "2026-10-02T23:00:10Z"}, "sweep_reclaim": None,
                "breakout_retest": None}
    revision = official_revision()
    revision["macro_releases"] = [deepcopy(revision["macro_releases"][0]) for _ in range(4)]
    for fact, title in zip(revision["macro_releases"], ("Nonfarm", "Unemployment", "Hourly earnings", "Euro area flash HICP")):
        fact["title"] = title
        # Match the complete official reader contract, including observation
        # provenance, rather than assuming a smaller hand-written news shape.
        fact.update(event_id=title, currency="USD", actual_value="29",
            basis="seasonally_adjusted_monthly_change", actual_status="VERIFIED",
            actual_available_at=fact["available_at"], known_at=fact["available_at"],
            schedule_source_url="https://www.forexfactory.com/calendar?week=this",
            published_at_source="scheduled_release_time",
            actual_method="DERIVED_FROM_OFFICIAL_SERIES", actual_is_estimate=False)
    payload["news_revisions"] = [revision, *payload["news_revisions"]]
    original = deepcopy(payload)
    fitted, report = _fit_prompt_payload(payload, "系统规则" * 620,
        context_length=8192, reserve=1024, signal_timeframe="15m")
    assert report["estimated_input_tokens"] + report["reserve_tokens"] + report["safety_margin_tokens"] <= 8192
    from tests.prompt_evidence_helpers import decode_macro_releases, decode_price_action
    projected_facts = decode_macro_releases(next(r for r in fitted["news_revisions"] if r.get("macro_releases")))
    assert len(projected_facts) == 4
    for projected, original_fact in zip(projected_facts, revision["macro_releases"]):
        for key in ("actual", "reference_period", "unit", "provider", "source_url", "published_at", "available_at", "forecast", "previous", "actual_method"):
            assert projected[key] == original_fact[key]
        assert projected["forecast_source"] == "Forex Factory"
        assert "actual_available_at" not in projected
    for symbol in fitted["allowed_instruments"]:
        for timeframe in ("15m", "1h"):
            assert decode_price_action(fitted["technical_context"], fitted["technical_context"][symbol]["timeframes"][timeframe]["price_action"]) == original["technical_context"][symbol]["timeframes"][timeframe]["price_action"]
    assert payload == original
