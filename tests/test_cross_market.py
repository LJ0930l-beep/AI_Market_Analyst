from datetime import datetime, timedelta, timezone

from core.analysis.cross_market import fetch_cross_market


def test_public_cross_market_sources_have_values_provenance_and_age() -> None:
    now = datetime(2026, 9, 25, 7, 15, tzinfo=timezone.utc)

    def fetch(url: str) -> dict:
        if url.endswith("/global"):
            return {"data": {"market_cap_percentage": {"btc": 58.5}, "updated_at": int((now - timedelta(minutes=2)).timestamp())}}
        ticker = "DXY" if "DX-Y.NYB" in url else "US10Y" if "%5ETNX" in url else "NQ"
        values = {"DXY": 101.2, "US10Y": 5.16, "NQ": 30882.75}
        age = timedelta(hours=6) if ticker == "US10Y" else timedelta(minutes=8)
        return {"chart": {"result": [{"meta": {"regularMarketPrice": values[ticker], "previousClose": values[ticker] - 1, "regularMarketTime": int((now - age).timestamp())}}]}}

    result = fetch_cross_market(now=now, get_json=fetch)
    items = {item["symbol"]: item for item in result["items"]}
    assert result["status"] == "AVAILABLE"
    assert items["DXY"]["status"] == "DELAYED"
    assert items["US10Y"]["status"] == "LAST_CLOSE"
    assert items["NQ"]["value"] == 30882.75
    assert items["BTC.D"]["value"] == 58.5
    assert all(item["source"] and item["as_of"] for item in items.values())


def test_failed_or_future_dated_sources_never_produce_a_normal_value() -> None:
    now = datetime(2026, 9, 25, 7, 15, tzinfo=timezone.utc)

    def fetch(url: str) -> dict:
        if url.endswith("/global"):
            return {"data": {"market_cap_percentage": {"btc": 58.5}, "updated_at": int((now + timedelta(hours=1)).timestamp())}}
        raise TimeoutError("feed unavailable")

    result = fetch_cross_market(now=now, get_json=fetch)
    assert result["status"] == "UNAVAILABLE"
    assert all(item["status"] == "UNAVAILABLE" and item["value"] is None for item in result["items"])
