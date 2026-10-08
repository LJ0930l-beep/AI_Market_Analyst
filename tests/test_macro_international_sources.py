from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.macro_international_sources import (
    fetch_international_actual,
    fetch_rba_text,
    international_reference_period,
    map_international_event,
    official_international_url,
)

AS_OF = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
EVENTS = {
    "japan": {"currency": "JPY", "title": "Tokyo Core CPI y/y", "event_time": "2026-10-01T23:30:00Z"},
    "swiss": {"currency": "CHF", "title": "CPI m/m", "event_time": "2026-10-01T06:30:00Z"},
    "germany": {"currency": "EUR", "title": "German Prelim CPI m/m", "event_time": "2026-09-30T06:29:00Z"},
    "abs_yoy": {"currency": "AUD", "title": "CPI y/y", "event_time": "2026-09-30T01:30:00Z"},
    "abs_mom": {"currency": "AUD", "title": "CPI m/m", "event_time": "2026-09-30T01:30:00Z"},
    "abs_trimmed": {"currency": "AUD", "title": "Trimmed Mean CPI m/m", "event_time": "2026-09-30T01:30:00Z"},
    "rba": {"currency": "AUD", "title": "Cash Rate", "event_time": "2026-09-29T04:30:00Z"},
    "canada": {"currency": "CAD", "title": "GDP m/m", "event_time": "2026-09-29T12:30:00Z"},
}


# Compact excerpts of official current releases used as fixtures. They mirror
# the release date, reference period, metric label, and units of the public
# sources linked in the reader; live reads are separately checked below.
JAPAN_INDEX = """
<p>東京都区部 2026年（令和8年）9月分 2026年10月2日公表</p>
"""
ESTAT_INDEX = """
<ul><li class="stat-dataset_list-detail-item">
中分類指数 前年同月比 2026年9月 2026-10-02
<a href="/stat-search/file-download?statInfId=123456789&amp;fileKind=1">CSV</a>
</li></ul>
"""
JAPAN_CSV = (
    'official Tokyo CPI series\n'
    'Time,"All items, less fresh food"\n'
    'unit,%\n'
    '202607,2.8\n'
    '202608,2.6\n'
    '202609,2.7\n'
    '202610,2.9\n'
).encode("cp932")

SWISS_INDEX = """
<a href="/en/newnsb/official-september-cpi">More about «Consumer prices remained stable in September»</a>
"""
SWISS_RELEASE = """
<p>Press release</p><p>Published on 1 October 2026</p>
<h1>Consumer prices remained stable in September</h1>
<p>The Consumer Price Index (CPI) remained unchanged in September 2026 compared with the previous month, at 101.5 points (December 2025 = 100).</p>
<p>These are the results of the Federal Statistical Office (FSO).</p>
"""
DESTATIS_INDEX = """
<a href="/EN/Press/2026/09/PE26_348_611.html">Press release No. 348 of 30 September 2026 Inflation rate of +3.3% expected in September 2026</a>
"""
DESTATIS_RELEASE = """
<p>Press release No. 348 of 30 September 2026</p>
<p>Consumer price index, September 2026: +0.6% on the previous month, provisional</p>
<p>Harmonised index of consumer prices, September 2026: +0.7% on the previous month</p>
"""


def _abs_release(*, wrong_period=False, wrong_units=False):
    reference = "July 2026" if wrong_period else "August 2026"
    prior_display = "June 26" if wrong_period else "July 26"
    period_display = "July 26" if wrong_period else "Aug 26"
    yoy = "0.7" if wrong_units else "4.0"
    mom = "0.4" if wrong_units else "0.7"
    return f"""
    <h1>Consumer Price Index, Australia, August 2026</h1>
    <p>Reference period {reference}</p><p>Released 30/09/2026</p>
    <p>Release date and time 30/09/2026 11:30am AEST</p>
    <p>In the 12 months to August 2026 the CPI rose 4.0%.</p>
    <table>
      <tr><th></th><th></th><th>Original</th><th>Seasonally adjusted</th></tr>
      <tr><th>Weighted average of eight capital cities</th>
          <th>{prior_display} to {period_display} (% change)</th>
          <th>Aug 25 to Aug 26 (% change)</th>
          <th>{prior_display} to {period_display} (% change)</th>
          <th>Aug 25 to Aug 26 (% change)</th></tr>
      <tr><td>All groups CPI</td><td>0.4</td><td>{yoy}</td><td>{mom}</td><td>3.9</td></tr>
    </table>
    <table>
      <tr><th>Analytical series</th><th>{prior_display} to {period_display} (% change)</th><th>Aug 25 to Aug 26 (% change)</th></tr>
      <tr><td>Trimmed mean</td><td>0.2</td><td>3.6</td></tr>
    </table>
    """


RBA_INDEX = """
<a href="/media-releases/2026/mr-26-27.html">29 September 2026</a>
"""
RBA_RELEASE = """
<h1>Statement by the Monetary Policy Board: Monetary Policy Decision</h1>
<p>Date 29 September 2026</p>
<p>At its meeting today, the Board decided to increase the cash rate target by 25 basis points to 4.60 per cent.</p>
"""
RBA_CONFERENCE = """
<h1>Media Conference: Monetary Policy Decision</h1>
<p>29 September 2026 – Sydney</p><p>Good afternoon. Today the Board decided to increase the cash rate target.</p>
"""
STATCAN_RELEASE = """
<p>Released: 2026-09-29</p>
<h1>Real GDP by industry July 2026</h1>
<p>Real GDP by industry July 2026 0.0% (monthly change)</p>
<p>Gross domestic product by industry, July 2026.</p>
"""


def _fixture_fetcher(overrides=None):
    payloads = {
        "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-t.html": JAPAN_INDEX,
        "https://www.e-stat.go.jp/stat-search/files?cycle=0&layout=datalist&page=1&tclass1=000001243880&tclass2=000001243881&tclass3=000001243884&tclass4=000001243888&tclass5val=0&toukei=00200573&tstat=000001243876": ESTAT_INDEX,
        "https://www.e-stat.go.jp/stat-search/file-download?statInfId=123456789&fileKind=1": JAPAN_CSV,
        "https://www.efd.admin.ch/en/newnsb": SWISS_INDEX,
        "https://www.efd.admin.ch/en/newnsb/official-september-cpi": SWISS_RELEASE,
        "https://www.destatis.de/EN/Press/press_node_2.html": DESTATIS_INDEX,
        "https://www.destatis.de/EN/Press/2026/09/PE26_348_611.html": DESTATIS_RELEASE,
        "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/latest-release": _abs_release(),
        "https://www.abs.gov.au/statistics/detailed-methodology-information/information-papers/introducing-consumer-price-indexs-new-monthly-time-series-extended-analysis-and-trimmed-mean": "The new Monthly Trimmed mean series is calculated from seasonally adjusted series using data up to October 2025.",
        "https://www.rba.gov.au/monetary-policy/int-rate-decisions/": RBA_INDEX,
        "https://www.rba.gov.au/media-releases/2026/mr-26-27.html": RBA_RELEASE,
        "https://www.rba.gov.au/speeches/2026/mc-gov-2026-09-29.html": RBA_CONFERENCE,
        "https://www150.statcan.gc.ca/n1/daily-quotidien/260929/dq260929a-eng.htm": STATCAN_RELEASE,
    }
    payloads.update(overrides or {})
    seen = []

    def fetch(url):
        seen.append(url)
        if url not in payloads:
            raise AssertionError(f"Unexpected source request: {url}")
        return payloads[url], AS_OF

    return fetch, seen


@pytest.mark.parametrize(
    ("name", "metric", "reference_period", "value", "basis"),
    [
        ("japan", "jpy_tokyo_core_cpi_yoy", "2026-09", "2.7", "year_over_year_not_seasonally_adjusted"),
        ("swiss", "chf_cpi_mom", "2026-09", "0", "month_over_month_not_seasonally_adjusted"),
        ("germany", "destatis_german_cpi_mom", "2026-09", "0.6", "month_over_month_not_seasonally_adjusted"),
        ("abs_yoy", "abs_cpi_yoy", "2026-08", "4", "year_over_year_not_seasonally_adjusted"),
        ("abs_mom", "abs_cpi_mom", "2026-08", "0.7", "seasonally_adjusted_month_over_month"),
        ("abs_trimmed", "abs_trimmed_mean_mom", "2026-08", "0.2", "seasonally_adjusted_month_over_month"),
        ("rba", "rba_cash_rate", "2026-09-29", "4.6", "cash_rate_target"),
        ("canada", "statcan_gdp_mom", "2026-07", "0", "seasonally_adjusted_month_over_month_chained_2017_dollars"),
    ],
)
def test_current_official_release_fixtures_return_period_bound_actuals(name, metric, reference_period, value, basis):
    fetch, seen = _fixture_fetcher()
    result = fetch_international_actual(EVENTS[name], fetch=fetch, now=AS_OF, clock=lambda: AS_OF)

    assert result["actual_status"] == "VERIFIED"
    assert result["actual_method"] == "OFFICIAL_REPORTED"
    assert result["actual_reference_period"] == reference_period
    assert result["actual_value"] == value
    assert result["actual_basis"] == basis
    assert result["actual_display"] == result["actual"]
    assert result["actual_provider"]
    assert result["actual_source_url"].startswith("https://")
    assert result["actual_published_at"] <= result["actual_available_at"]
    assert len(seen) <= 3
    assert metric == map_international_event(EVENTS[name])


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (EVENTS["japan"], "2026-09"),
        (EVENTS["swiss"], "2026-09"),
        (EVENTS["germany"], "2026-09"),
        (EVENTS["abs_yoy"], "2026-08"),
        (EVENTS["rba"], "2026-09-29"),
        (EVENTS["canada"], "2026-07"),
    ],
)
def test_release_specific_reference_periods(event, expected):
    metric = map_international_event(event)
    assert international_reference_period(metric, event["event_time"]) == expected


def test_exact_title_and_currency_mapping_rejects_substitute_series():
    assert map_international_event({"currency": "EUR", "title": "German Prelim CPI m/m"}) == "destatis_german_cpi_mom"
    assert map_international_event({"currency": "EUR", "title": "German Prelim HICP m/m"}) is None
    assert map_international_event({"currency": "JPY", "title": "Japan CPI y/y"}) is None
    assert map_international_event({"currency": "AUD", "title": "Monthly CPI Indicator y/y"}) is None
    assert map_international_event({"currency": "AUD", "title": "Cash Rate", "event_time": "2026-09-29T04:30:00Z"}) == "rba_cash_rate"


def test_rejects_future_events_before_source_io():
    future = {**EVENTS["swiss"], "event_time": "2026-10-04T06:30:00Z"}
    with pytest.raises(ValueError, match="INTERNATIONAL_RELEASE_NOT_YET_AVAILABLE"):
        fetch_international_actual(future, fetch=lambda _url: pytest.fail("must not fetch"), now=AS_OF)


def test_rejects_wrong_release_date_and_hicp_substitution():
    wrong_date = SWISS_RELEASE.replace("Published on 1 October 2026", "Published on 2 October 2026")
    fetch, _ = _fixture_fetcher({"https://www.efd.admin.ch/en/newnsb/official-september-cpi": wrong_date})
    with pytest.raises(ValueError, match="INTERNATIONAL_RELEASE_DATE_MISMATCH"):
        fetch_international_actual(EVENTS["swiss"], fetch=fetch, now=AS_OF)

    hicp_only = "<p>Press release No. 348 of 30 September 2026</p><p>Harmonised index of consumer prices, September 2026: +0.7% on the previous month</p>"
    fetch, _ = _fixture_fetcher({"https://www.destatis.de/EN/Press/2026/09/PE26_348_611.html": hicp_only})
    with pytest.raises(ValueError, match="INTERNATIONAL_NATIONAL_CPI_SECTION_MISSING"):
        fetch_international_actual(EVENTS["germany"], fetch=fetch, now=AS_OF)


def test_abs_rejects_wrong_period_and_basis():
    wrong_period = _abs_release(wrong_period=True)
    fetch, _ = _fixture_fetcher({"https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/latest-release": wrong_period})
    with pytest.raises(ValueError, match="INTERNATIONAL_REFERENCE_PERIOD_MISMATCH"):
        fetch_international_actual(EVENTS["abs_mom"], fetch=fetch, now=AS_OF)

    wrong_basis = _abs_release(wrong_units=True).replace("July 26 to Aug 26 (% change)", "July 26 to Aug 26 (index points)")
    fetch, _ = _fixture_fetcher({"https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/latest-release": wrong_basis})
    with pytest.raises(ValueError, match="INTERNATIONAL_ABS_CPI_BASIS_MISMATCH"):
        fetch_international_actual(EVENTS["abs_mom"], fetch=fetch, now=AS_OF)


def test_fetch_limits_official_urls_and_response_size():
    fetch, seen = _fixture_fetcher()
    result = fetch_international_actual(EVENTS["swiss"], fetch=fetch, now=AS_OF)
    assert result["actual_status"] == "VERIFIED"
    assert all(official_international_url(EVENTS["swiss"], url) for url in seen)
    assert not official_international_url(EVENTS["swiss"], "https://example.com/fake")
    assert not official_international_url(EVENTS["swiss"], "http://www.efd.admin.ch/en/newnsb/wNfni1DvzKuQ")
    assert not official_international_url(EVENTS["swiss"], "https://www.rba.gov.au/media-releases/2026/mr-26-27.html")
    with pytest.raises(ValueError, match="INTERNATIONAL_RESPONSE_TOO_LARGE"):
        fetch_international_actual(EVENTS["swiss"], fetch=lambda _url: b"x" * 1_000_001, now=AS_OF)


def test_rba_statement_and_press_conference_are_qualitative_only():
    event = {**EVENTS["rba"], "title": "RBA Rate Statement"}
    fetch, seen = _fixture_fetcher()
    result = fetch_rba_text(event, fetch=fetch, now=AS_OF, clock=lambda: AS_OF)
    assert result["qualitative_status"] == "OFFICIAL_TEXT_AVAILABLE"
    assert result["qualitative_provider"] == "Reserve Bank of Australia (RBA)"
    assert "Rate Decision" in result["qualitative_title"] or "Decision" in result["qualitative_title"]
    assert result["qualitative_published_at"] == "2026-09-29T04:30:00+00:00"
    assert "actual" not in result and "actual_value" not in result
    assert len(result["qualitative_summary"]) <= 600
    assert len(seen) == 3
    assert all(official_international_url(event, url) for url in [result["qualitative_source_url"], result["qualitative_conference_url"]])

    press = {**event, "title": "RBA Press Conference"}
    fetch, _ = _fixture_fetcher()
    assert fetch_rba_text(press, fetch=fetch, now=AS_OF)["qualitative_status"] == "OFFICIAL_TEXT_AVAILABLE"
