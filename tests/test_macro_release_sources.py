import zlib
from datetime import datetime, timezone

import pytest

from core.macro_release_sources import (
    ADP_RELEASES_INDEX,
    BEA_CURRENT_RELEASES,
    BEA_GDP_PRICE_INDEX,
    CB_CONFIDENCE_PAGE,
    DOL_WEEKLY_CLAIMS_PDF,
    ISM_PRNEWSWIRE_CHANNEL,
    MAX_RESPONSE_BYTES,
    _parse_dol_pdf,
    fetch_release_actual,
    map_release_event,
    official_release_url,
)

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
COMPLETED = NOW.isoformat()


def _event(title, at, currency="USD"):
    return {"title": title, "currency": currency, "event_time": at}


def _reader(fixtures):
    def fetch(url):
        assert url in fixtures, f"unexpected official request: {url}"
        return fixtures[url], COMPLETED
    return fetch


BEA_INDEX = """
<html><body><a href="/news/2026/gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-2nd">
GDP (Third Estimate), Industries, Corporate Profits, State GDP, and State Personal Income, 2nd Quarter 2026
</a><a href="/news/2026/personal-income-and-outlays-august-2026">Personal Income and Outlays, August 2026</a></body></html>
"""
GDP_ARTICLE = """
<html><body><p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Wednesday, September 30, 2026</p>
<h1>GDP (Third Estimate), Industries, Corporate Profits, State GDP, and State Personal Income, 2nd Quarter 2026</h1>
<p>Real gross domestic product (GDP) increased at an annual rate of 2.2 percent in the second quarter of 2026 (April, May, and June), according to the third estimate.</p></body></html>
"""
PIO_ARTICLE = """
<html><body><p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Wednesday, September 30, 2026</p>
<h1>Personal Income and Outlays, August 2026</h1>
<p>Excluding food and energy, the PCE price index increased 0.2 percent.</p></body></html>
"""
GDP_PRICE_PAGE = """
<html><body><h1>GDP Price Index</h1><table><thead><tr><th colspan="2">Quarterly - Percent Change from Preceding Quarter</th></tr></thead>
<tbody><tr><td>Q2 2026 (3rd)</td><td>+6.1%</td></tr><tr><td>Q1 2026</td><td>+3.2%</td></tr></tbody></table></body></html>
"""
ISM_PRNEWSWIRE_ARTICLE = """
<html><head><script type="application/ld+json">
{"@type":"NewsArticle","headline":"Manufacturing PMI® at 54.5%; September 2026 ISM® Manufacturing PMI® Report","datePublished":"2026-10-01T10:00:00-04:00"}
</script></head><body>
<h1>Manufacturing PMI® at 54.5%; September 2026 ISM® Manufacturing PMI® Report</h1>
<p>News provided by Institute for Supply Management</p><p>Oct 01, 2026, 10:00 ET</p>
<p>The Manufacturing PMI® registered 54.5 percent in September, 0.1 percentage point below the August figure.</p>
</body></html>
"""
ISM_PRNEWSWIRE_ITEM = (
    "https://www.prnewswire.com/news-releases/"
    "manufacturing-pmi-at-54-5-september-2026-ism-manufacturing-pmi-report-302894520.html"
)
ISM_PRNEWSWIRE_INDEX = f"""
<html><body><a href="{ISM_PRNEWSWIRE_ITEM.removeprefix('https://www.prnewswire.com')}">Oct 01, 2026, 10:00 ET Manufacturing PMI® at 54.5%;
September 2026 ISM® Manufacturing PMI® Report</a></body></html>
"""


def _minimal_pdf(release_and_claims: str) -> bytes:
    escaped = release_and_claims.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"[({escaped})] TJ".encode("latin-1")
    return b"%PDF-1.7\nstream\n" + zlib.compress(stream) + b"\nendstream\n%%EOF"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Final GDP q/q", "bea_final_gdp_qoq"),
        ("Final GDP Price Index q/q", "bea_final_gdp_price_index_qoq"),
        ("Core PCE Price Index m/m", "bea_core_pce_mom"),
        ("Unemployment Claims", "dol_unemployment_claims"),
        ("ISM Manufacturing PMI", "ism_manufacturing_pmi"),
        ("ADP Non-Farm Employment Change", "adp_nonfarm_employment_change"),
        ("CB Consumer Confidence", "conference_board_consumer_confidence"),
        ("JOLTS Job Openings", "bls_jolts_job_openings"),
    ],
)
def test_exact_title_and_currency_mapping(title, expected):
    assert map_release_event(_event(title, "2026-10-03T12:00:00+00:00")) == expected
    assert map_release_event(_event(title, "2026-10-03T12:00:00+00:00", "EUR")) is None
    assert map_release_event(_event(title + " Extra", "2026-10-03T12:00:00+00:00")) is None


@pytest.mark.parametrize(
    ("event", "fixtures", "expected_value", "expected_period", "unit"),
    [
        (
            _event("Final GDP q/q", "2026-09-30T12:30:00+00:00"),
            {BEA_CURRENT_RELEASES: BEA_INDEX,
             "https://www.bea.gov/news/2026/gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-2nd": GDP_ARTICLE},
            "2.2", "2026-Q2", "percent",
        ),
        (
            _event("Final GDP Price Index q/q", "2026-09-30T12:30:00+00:00"),
            {BEA_CURRENT_RELEASES: BEA_INDEX,
             "https://www.bea.gov/news/2026/gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-2nd": GDP_ARTICLE,
             BEA_GDP_PRICE_INDEX: GDP_PRICE_PAGE},
            "6.1", "2026-Q2", "percent",
        ),
        (
            _event("Core PCE Price Index m/m", "2026-09-30T12:30:00+00:00"),
            {BEA_CURRENT_RELEASES: BEA_INDEX,
             "https://www.bea.gov/news/2026/personal-income-and-outlays-august-2026": PIO_ARTICLE},
            "0.2", "2026-08", "percent",
        ),
        (
            _event("Unemployment Claims", "2026-10-01T12:30:00+00:00"),
            {DOL_WEEKLY_CLAIMS_PDF: _minimal_pdf(
                "TRANSMISSION OF MATERIALS IN THIS RELEASE IS EMBARGOED UNTIL 8:30 A.M. (Eastern) Thursday, October 1, 2026. "
                "In the week ending September 26, the advance figure for seasonally adjusted initial claims was 197,000, a decrease."
            )},
            "197000", "2026-09-26", "persons",
        ),
        (
            _event("ISM Manufacturing PMI", "2026-10-01T14:00:00+00:00"),
            {
                "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/september/":
                    "<html><body><h1>September 2026 ISM Manufacturing PMI Report</h1><p>The report was issued today.</p><p>The Manufacturing PMI registered 54.5 percent in September.</p></body></html>",
            },
            "54.5", "2026-09", "diffusion_index",
        ),
        (
            _event("ADP Non-Farm Employment Change", "2026-09-30T12:15:00+00:00"),
            {
                ADP_RELEASES_INDEX: "<a href=\"/2026-09-30-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-90,000-Jobs-in-September\">ADP National Employment Report: Private-Sector Employment Increased by 90,000 Jobs in September 2026</a>",
                "https://mediacenter.adp.com/2026-09-30-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-90,000-Jobs-in-September":
                    "<!-- ITEMDATE: 2026-09-30 08:15:00 EDT --><html><body><h1>ADP National Employment Report</h1><p>September 2026 Report Highlights</p><p>Change in U.S. Private Employment: 90,000</p></body></html>",
            },
            "90000", "2026-09", "persons",
        ),
        (
            _event("CB Consumer Confidence", "2026-09-29T14:00:00+00:00"),
            {CB_CONFIDENCE_PAGE: "<html><body><h1>US Consumer Confidence</h1><p>Updated: Tuesday, September 29, 2026</p><p>The Conference Board Consumer Confidence Index fell by 6.7 points to 81.9 (1985=100) in September, down from 88.6 in August.</p></body></html>"},
            "81.9", "2026-09", "index_1985_100",
        ),
        (
            _event("JOLTS Job Openings", "2026-09-29T14:00:00+00:00"),
            {"https://www.bls.gov/news.release/archives/jolts_09292026.htm": """
                <html><body><p>For release 10:00 a.m. (ET) Tuesday, September 29, 2026</p>
                <p>JOB OPENINGS AND LABOR TURNOVER — AUGUST 2026</p>
                <table><tr><th>Industry and region</th><th>Levels (in thousands)</th><th>Rates</th></tr>
                <tr><th>Aug.<br>2025</th><th>May<br>2026</th><th>June<br>2026</th><th>July<br>2026</th><th>Aug.<br>2026(p)</th></tr>
                <tr><td>Total</td><td>6919</td><td>7537</td><td>7182</td><td>7335</td><td>7079</td></tr></table></body></html>
            """},
            "7079", "2026-08", "thousand_job_openings",
        ),
    ],
)
def test_official_current_release_shapes(event, fixtures, expected_value, expected_period, unit):
    actual = fetch_release_actual(event, fetch=_reader(fixtures), now=NOW, clock=lambda: NOW)
    assert actual["actual_status"] == "VERIFIED"
    assert actual["actual_method"] == "OFFICIAL_REPORTED"
    assert actual["actual_is_estimate"] is False
    assert actual["actual_value"] == expected_value
    assert actual["actual_reference_period"] == expected_period
    assert actual["actual_unit"] == unit
    assert actual["actual_source_url"].startswith("https://")
    assert actual["actual_published_at"] <= actual["actual_available_at"]
    assert actual["actual_available_at"] == COMPLETED


def test_dol_real_public_pdf_release_readback_shape():
    sample = _minimal_pdf(
        "TRANSMISSION OF MATERIALS IN THIS RELEASE IS EMBARGOED UNTIL 8:30 A.M. (Eastern) Thursday, October 1, 2026. "
        "In the week ending September 26, the advance figure for seasonally adjusted initial claims was 197,000, a decrease."
    )
    value, published = _parse_dol_pdf(sample, "2026-09-26", datetime(2026, 10, 1).date())
    assert value == "197000"
    assert published.isoformat() == "2026-10-01T12:30:00+00:00"


def test_ism_provider_channel_fallback_binds_publisher_period_and_release_timestamp():
    event = _event("ISM Manufacturing PMI", "2026-10-01T14:00:00+00:00")
    official_report = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/september/"
    fixtures = {
        ISM_PRNEWSWIRE_CHANNEL: ISM_PRNEWSWIRE_INDEX,
        ISM_PRNEWSWIRE_ITEM: ISM_PRNEWSWIRE_ARTICLE,
    }

    def fetch(url):
        if url == official_report:
            raise ValueError("OFFICIAL_SOURCE_REDIRECT_REJECTED")
        assert url in fixtures
        return fixtures[url], COMPLETED

    actual = fetch_release_actual(event, fetch=fetch, now=NOW, clock=lambda: NOW)
    assert actual["actual_status"] == "VERIFIED"
    assert actual["actual_value"] == "54.5"
    assert actual["actual_reference_period"] == "2026-09"
    assert actual["actual_published_at"] == "2026-10-01T14:00:00+00:00"
    assert actual["actual_provider"] == "Institute for Supply Management"
    assert actual["actual_source_url"] == ISM_PRNEWSWIRE_ITEM
    assert official_release_url(event, actual["actual_source_url"])


@pytest.mark.parametrize(
    ("article", "error"),
    [
        (ISM_PRNEWSWIRE_ARTICLE.replace("News provided by Institute for Supply Management", "News provided by Unrelated Publisher"), "OFFICIAL_PROVIDER_MISMATCH"),
        (ISM_PRNEWSWIRE_ARTICLE.replace("2026-10-01T10:00:00-04:00", "2026-10-02T10:00:00-04:00"), "OFFICIAL_PERIOD_MISMATCH:ISM_PRNEWSWIRE_RELEASE_DATE"),
    ],
)
def test_ism_provider_fallback_rejects_unmatched_publisher_or_release_day(article, error):
    event = _event("ISM Manufacturing PMI", "2026-10-01T14:00:00+00:00")
    official_report = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/september/"
    fixtures = {ISM_PRNEWSWIRE_CHANNEL: ISM_PRNEWSWIRE_INDEX, ISM_PRNEWSWIRE_ITEM: article}

    def fetch(url):
        if url == official_report:
            raise ValueError("OFFICIAL_SOURCE_REDIRECT_REJECTED")
        return fixtures[url], COMPLETED

    with pytest.raises(ValueError, match=error):
        fetch_release_actual(event, fetch=fetch, now=NOW, clock=lambda: NOW)


def test_wrong_release_period_is_rejected_before_value_is_verified():
    wrong = _minimal_pdf(
        "TRANSMISSION OF MATERIALS IN THIS RELEASE IS EMBARGOED UNTIL 8:30 A.M. (Eastern) Thursday, October 1, 2026. "
        "In the week ending September 19, the advance figure for seasonally adjusted initial claims was 196,000."
    )
    with pytest.raises(ValueError, match="PERIOD_MISMATCH"):
        _parse_dol_pdf(wrong, "2026-09-26", datetime(2026, 10, 1).date())


def test_source_allowlist_and_event_specific_final_url_check():
    jolts = _event("JOLTS Job Openings", "2026-09-29T14:00:00+00:00")
    assert official_release_url(jolts, "https://www.bls.gov/news.release/archives/jolts_09292026.htm")
    assert not official_release_url(jolts, "https://api.bls.gov/publicAPI/v1/timeseries/data/")
    assert not official_release_url(jolts, "https://www.bls.gov/news.release/archives/jolts_09282026.htm")
    assert not official_release_url(jolts, "https://attacker.example/news.release/archives/jolts_09292026.htm")
    assert not official_release_url(jolts, "https://www.bls.gov/news.release/archives/jolts_09292026.htm?next=https://attacker.example")
    ism = _event("ISM Manufacturing PMI", "2026-10-01T14:00:00+00:00")
    assert official_release_url(ism, ISM_PRNEWSWIRE_ITEM)
    assert not official_release_url(ism, ISM_PRNEWSWIRE_ITEM.replace("september-2026", "august-2026"))
    assert not official_release_url(ism, "https://attacker.example/news-releases/manufacturing-pmi-at-54-5-september-2026-ism-manufacturing-pmi-report-302894520.html")


def test_request_bounds_reject_unknown_hosts_oversize_and_future_values():
    event = _event("Unemployment Claims", "2026-10-01T12:30:00+00:00")
    with pytest.raises(ValueError, match="RESPONSE_TOO_LARGE"):
        fetch_release_actual(event, fetch=lambda _url: "x" * (MAX_RESPONSE_BYTES + 1), now=NOW, clock=lambda: NOW)
    with pytest.raises(ValueError, match="RELEASE_NOT_YET_KNOWN"):
        fetch_release_actual(event, now=datetime(2026, 9, 30, tzinfo=timezone.utc), clock=lambda: NOW)


def test_dol_pdf_decompression_is_bounded_and_linear_for_bad_arrays():
    compressed = b"%PDF-1.7\nstream\n" + zlib.compress(b"[" * 13_000_000) + b"\nendstream"
    with pytest.raises(ValueError, match="DECOMPRESSION_LIMIT"):
        _parse_dol_pdf(compressed, "2026-09-26", datetime(2026, 10, 1).date())

    # A reasonably large but bounded malformed text array must terminate.
    malformed = b"%PDF-1.7\nstream\n" + zlib.compress(b"[" * 120_000) + b"\nendstream"
    with pytest.raises(ValueError, match="PUBLISHED_AT_NOT_FOUND"):
        _parse_dol_pdf(malformed, "2026-09-26", datetime(2026, 10, 1).date())
