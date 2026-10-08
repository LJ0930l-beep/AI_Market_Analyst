"""Period-bound official actuals for a narrow set of international releases.

The economic calendar is only used to identify an event and its scheduled time.
Every value in this module is read from a dated, official release or official
machine-readable statistical series.  No event-provided URL is ever fetched.
"""

from __future__ import annotations

import io
import re
import socket
import ssl
from csv import reader as csv_reader
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

try:  # requests is an existing project dependency and supplies certifi.
    import certifi
except ImportError:  # pragma: no cover - standard trust store remains verified.
    certifi = None


MAX_SOURCE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 12
MAX_REQUESTS_PER_FETCH = 4
OFFICIAL_HOSTS = frozenset({
    "www.stat.go.jp",
    "www.e-stat.go.jp",
    "www.efd.admin.ch",
    "www.destatis.de",
    "www.abs.gov.au",
    "www.rba.gov.au",
    "www150.statcan.gc.ca",
})
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.7,de;q=0.6",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

JAPAN_RELEASE_URL = "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-t.html"
JAPAN_ESTAT_LIST_URL = (
    "https://www.e-stat.go.jp/stat-search/files?cycle=0&layout=datalist&page=1"
    "&tclass1=000001243880&tclass2=000001243881&tclass3=000001243884"
    "&tclass4=000001243888&tclass5val=0&toukei=00200573&tstat=000001243876"
)
SWISS_NEWS_INDEX_URL = "https://www.efd.admin.ch/en/newnsb"
DESTATIS_PRESS_INDEX_URL = "https://www.destatis.de/EN/Press/press_node_2.html"
ABS_LATEST_RELEASE_URL = (
    "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/"
    "consumer-price-index-australia/latest-release"
)
ABS_TRIMMED_MEAN_METHODOLOGY_URL = (
    "https://www.abs.gov.au/statistics/detailed-methodology-information/information-papers/"
    "introducing-consumer-price-indexs-new-monthly-time-series-extended-analysis-and-trimmed-mean"
)
RBA_DECISIONS_INDEX_URL = "https://www.rba.gov.au/monetary-policy/int-rate-decisions/"
RBA_MEDIA_RELEASE_INDEX_URL = "https://www.rba.gov.au/media-releases/"
STATCAN_RELEASE_ROOT = "https://www150.statcan.gc.ca/n1/daily-quotidien/"

_CURRENCY_ALIASES = {
    "jpy": "jpy", "jp": "jpy", "japan": "jpy", "japanese": "jpy",
    "chf": "chf", "ch": "chf", "switzerland": "chf", "swiss": "chf",
    "eur": "eur", "de": "eur", "germany": "eur", "german": "eur",
    "aud": "aud", "au": "aud", "australia": "aud", "australian": "aud",
    "cad": "cad", "ca": "cad", "canada": "cad", "canadian": "cad",
}

_EVENT_SPECS = {
    "jpy_tokyo_core_cpi_yoy": {
        "currency": "jpy", "title": "tokyo core cpi y/y", "provider": "Statistics Bureau of Japan",
        "zone": "Asia/Tokyo", "lag": 1,
    },
    "chf_cpi_mom": {
        "currency": "chf", "title": "cpi m/m", "provider": "Swiss Federal Statistical Office (FSO)",
        "zone": "Europe/Zurich", "lag": 1,
    },
    "destatis_german_cpi_mom": {
        "currency": "eur", "title": "german prelim cpi m/m", "provider": "German Federal Statistical Office (Destatis)",
        "zone": "Europe/Berlin", "lag": 0,
    },
    "abs_cpi_yoy": {
        "currency": "aud", "title": "cpi y/y", "provider": "Australian Bureau of Statistics (ABS)",
        "zone": "Australia/Sydney", "lag": 1,
    },
    "abs_cpi_mom": {
        "currency": "aud", "title": "cpi m/m", "provider": "Australian Bureau of Statistics (ABS)",
        "zone": "Australia/Sydney", "lag": 1,
    },
    "abs_trimmed_mean_mom": {
        "currency": "aud", "title": "trimmed mean cpi m/m", "provider": "Australian Bureau of Statistics (ABS)",
        "zone": "Australia/Sydney", "lag": 1,
    },
    "rba_cash_rate": {
        "currency": "aud", "title": "cash rate", "provider": "Reserve Bank of Australia (RBA)",
        "zone": "Australia/Sydney", "lag": None,
    },
    "statcan_gdp_mom": {
        "currency": "cad", "title": "gdp m/m", "provider": "Statistics Canada",
        "zone": "America/Toronto", "lag": 2,
    },
}

_TITLE_TO_METRIC = {
    (spec["currency"], spec["title"]): metric
    for metric, spec in _EVENT_SPECS.items()
}


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("INTERNATIONAL_EVENT_TIME_INVALID") from exc
    if result.tzinfo is None:
        raise ValueError("INTERNATIONAL_EVENT_TIME_INVALID")
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _month_shift(year: int, month: int, delta: int) -> str:
    absolute = year * 12 + month - 1 + delta
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _normalized_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def map_international_event(event: dict) -> str | None:
    """Map only the exact requested currency/title pairs to supported metrics."""
    if not isinstance(event, dict):
        return None
    currency = re.sub(r"[^a-z]", "", str(event.get("currency", "")).lower())
    canonical_currency = _CURRENCY_ALIASES.get(currency)
    title = _normalized_text(event.get("title"))
    return _TITLE_TO_METRIC.get((canonical_currency, title))


def international_reference_period(metric: str, event_time) -> str:
    """Return the release-specific reporting month or dated policy decision."""
    spec = _EVENT_SPECS.get(metric)
    if spec is None:
        raise ValueError("INTERNATIONAL_METRIC_UNMAPPED")
    local = _utc(event_time).astimezone(ZoneInfo(spec["zone"]))
    if metric == "rba_cash_rate":
        return local.date().isoformat()
    local_month = _month_shift(local.year, local.month, 0)
    if spec["lag"] is not None:
        return _month_shift(local.year, local.month, -spec["lag"])
    # Destatis' flash estimate is a late-month release for the current month.
    return local_month if local.day >= 20 else _month_shift(local.year, local.month, -1)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("INTERNATIONAL_REDIRECT_REJECTED")


def _validate_official_url(url: str) -> None:
    parsed = urlparse(str(url))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.hostname.lower() not in OFFICIAL_HOSTS):
        raise ValueError("INTERNATIONAL_SOURCE_URL_REJECTED")


_EVENT_URL_PATHS = {
    "jpy_tokyo_core_cpi_yoy": {
        "www.stat.go.jp": ("/data/cpi/sokuhou/tsuki/",),
        "www.e-stat.go.jp": ("/stat-search/files", "/stat-search/file-download"),
    },
    "chf_cpi_mom": {"www.efd.admin.ch": ("/en/newnsb",)},
    "destatis_german_cpi_mom": {"www.destatis.de": ("/EN/Press/",)},
    "abs_cpi_yoy": {"www.abs.gov.au": ("/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/",)},
    "abs_cpi_mom": {"www.abs.gov.au": ("/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/",)},
    "abs_trimmed_mean_mom": {"www.abs.gov.au": (
        "/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia/",
        "/statistics/detailed-methodology-information/information-papers/",
    )},
    "rba_cash_rate": {"www.rba.gov.au": ("/monetary-policy/int-rate-decisions/", "/media-releases/", "/speeches/")},
    "statcan_gdp_mom": {"www150.statcan.gc.ca": ("/n1/daily-quotidien/",)},
}


def _is_rba_text_event(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    currency = re.sub(r"[^a-z]", "", str(event.get("currency", "")).lower())
    title = _normalized_text(event.get("title"))
    return _CURRENCY_ALIASES.get(currency) == "aud" and title in {
        "rba rate statement", "rate statement", "rba press conference", "press conference",
    }


def official_international_url(event: dict, url: str) -> bool:
    """Return whether a URL belongs to this event's fixed official source paths."""
    metric = map_international_event(event)
    if metric is None and _is_rba_text_event(event):
        metric = "rba_cash_rate"
    if metric is None:
        return False
    try:
        _validate_official_url(url)
    except (TypeError, ValueError):
        return False
    parsed = urlparse(str(url))
    host = (parsed.hostname or "").lower()
    paths = _EVENT_URL_PATHS.get(metric, {}).get(host, ())
    if not any(parsed.path.startswith(prefix) for prefix in paths):
        return False
    if host == "www.e-stat.go.jp" and parsed.path == "/stat-search/file-download":
        from urllib.parse import parse_qs
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            return False
        return (set(query) == {"statInfId", "fileKind"}
                and len(query["statInfId"]) == 1 and query["statInfId"][0].isdigit()
                and query["fileKind"] == ["1"])
    return True


def _ssl_context():
    context = ssl.create_default_context()
    if certifi is not None:
        # Keep the Windows/system trust roots and add certifi's current roots.
        context.load_verify_locations(cafile=certifi.where())
    return context


def _network_error(exc: URLError) -> ValueError:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLError):
        return ValueError("INTERNATIONAL_SOURCE_TLS_FAILED")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return ValueError("INTERNATIONAL_SOURCE_TIMEOUT")
    return ValueError("INTERNATIONAL_SOURCE_NETWORK_FAILED")


def _http_fetch(url: str):
    """Fetch one fixed official endpoint with verified TLS, no redirects, and a byte cap."""
    _validate_official_url(url)
    request = Request(url, headers=_BROWSER_HEADERS)
    from urllib.request import HTTPSHandler
    opener = build_opener(_NoRedirect(), HTTPSHandler(context=_ssl_context()))
    try:
        response = opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("INTERNATIONAL_REDIRECT_REJECTED") from exc
        raise ValueError(f"INTERNATIONAL_SOURCE_HTTP_{exc.code}") from exc
    except URLError as exc:
        raise _network_error(exc) from exc
    with response:
        if response.geturl() != url:
            raise ValueError("INTERNATIONAL_REDIRECT_REJECTED")
        raw = response.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("INTERNATIONAL_RESPONSE_TOO_LARGE")
    return raw, datetime.now(UTC)


class _BoundFetcher:
    def __init__(self, fetch, clock):
        self.fetcher = fetch or _http_fetch
        self.clock = clock
        self.calls = 0
        self.completions: list[datetime] = []

    def __call__(self, url: str):
        _validate_official_url(url)
        self.calls += 1
        if self.calls > MAX_REQUESTS_PER_FETCH:
            raise ValueError("INTERNATIONAL_REQUEST_LIMIT")
        try:
            result = self.fetcher(url)
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"INTERNATIONAL_SOURCE_FETCH_FAILED:{type(exc).__name__}") from exc
        if isinstance(result, tuple) and len(result) == 2:
            body, completed_at = result
            completed_at = _utc(completed_at)
        else:
            body, completed_at = result, _utc(self.clock())
        if isinstance(body, str):
            size = len(body.encode("utf-8"))
        elif isinstance(body, (bytes, bytearray)):
            size = len(body)
            body = bytes(body)
        else:
            raise TypeError("INTERNATIONAL_SOURCE_BODY_INVALID")
        if size > MAX_SOURCE_BYTES:
            raise ValueError("INTERNATIONAL_RESPONSE_TOO_LARGE")
        self.completions.append(completed_at)
        return body, completed_at


class _Document(HTMLParser):
    """Small standard-library HTML reader for text, anchors, and official tables."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.tables: list[list[list[str]]] = []
        self._skip_depth = 0
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if self._skip_depth:
            return
        if tag.lower() == "a":
            self._anchor_href = str(attrs.get("href", ""))
            self._anchor_text = []
        elif tag.lower() == "table" and self._table is None:
            self._table = []
        elif tag.lower() == "tr" and self._table is not None:
            self._row = []
        elif tag.lower() in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag.lower() in {"td", "th"} and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag.lower() == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag.lower() == "a" and self._anchor_href is not None:
            label = re.sub(r"\s+", " ", " ".join(self._anchor_text)).strip()
            self.links.append((self._anchor_href, label))
            self._anchor_href = None
            self._anchor_text = []

    def handle_data(self, data):
        if self._skip_depth:
            return
        if data.strip():
            self.parts.append(data.strip())
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def _document(body, *, encoding="utf-8") -> _Document:
    if isinstance(body, bytes):
        try:
            body = body.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("INTERNATIONAL_SOURCE_ENCODING_INVALID") from exc
    doc = _Document()
    doc.feed(body)
    return doc


def _event_publication_date(event_time: datetime, metric: str) -> date:
    return _utc(event_time).astimezone(ZoneInfo(_EVENT_SPECS[metric]["zone"])).date()


def _date_match(text: str, published_date: date, source: str) -> None:
    rendered = f"{published_date.day} {published_date.strftime('%B %Y')}"
    if source == "japan":
        pattern = rf"{published_date.year}\s*年\s*{published_date.month}\s*月\s*{published_date.day}\s*日\s*公表"
    elif source == "swiss":
        pattern = rf"Published on\s+{re.escape(rendered)}"
    elif source == "destatis":
        pattern = rf"Press release No\.\s*\d+\s+of\s+{re.escape(rendered)}"
    elif source == "abs":
        pattern = rf"Released\s*{published_date:%d/%m/%Y}"
    elif source == "rba":
        pattern = rf"Date\s+{re.escape(rendered)}"
    elif source == "statcan":
        pattern = rf"Released:\s*{published_date:%Y-%m-%d}"
    else:
        raise ValueError("INTERNATIONAL_SOURCE_UNMAPPED")
    if not re.search(pattern, text, flags=re.IGNORECASE):
        raise ValueError("INTERNATIONAL_RELEASE_DATE_MISMATCH")


def _month_names(reference_period: str) -> tuple[str, str, int, int]:
    year, month = (int(value) for value in reference_period.split("-"))
    dt = date(year, month, 1)
    return dt.strftime("%B"), dt.strftime("%b"), year, month


def _month_pattern(reference_period: str) -> str:
    full, _short, year, _month = _month_names(reference_period)
    return rf"{re.escape(full)}\s+{year}"


class _EStatDatasetParser(HTMLParser):
    """Collect one e-Stat result item and its same-item download links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._item: dict | None = None
        self._anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "li" and "stat-dataset_list-detail-item" in str(attrs.get("class", "")):
            self._item = {"parts": [], "links": []}
        elif self._item is not None and tag.lower() == "a":
            self._anchor = {"href": str(attrs.get("href", "")), "parts": []}

    def handle_data(self, data):
        if self._item is not None:
            self._item["parts"].append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)

    def handle_endtag(self, tag):
        if self._item is None:
            return
        if tag.lower() == "a" and self._anchor is not None:
            label = re.sub(r"\s+", " ", " ".join(self._anchor["parts"])).strip()
            self._item["links"].append((self._anchor["href"], label))
            self._anchor = None
        elif tag.lower() == "li":
            self._item["text"] = re.sub(r"\s+", " ", " ".join(self._item["parts"])).strip()
            self.items.append(self._item)
            self._item = None


def _resolve_estat_core_csv(body, reference_period: str, publication_date: date) -> str:
    """Find and validate the latest official Tokyo-wards all-less-fresh-food series."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="strict")
    parser = _EStatDatasetParser()
    parser.feed(body)
    ref_year, ref_month = (int(value) for value in reference_period.split("-"))
    expected_release = publication_date.isoformat()
    period_text = f"{ref_year}年{ref_month}月"
    candidates = [
        item for item in parser.items
        if "中分類指数" in item.get("text", "")
        and "前年同月比" in item.get("text", "")
        and period_text in item.get("text", "")
        and expected_release in item.get("text", "")
    ]
    if len(candidates) != 1:
        raise ValueError("INTERNATIONAL_ESTAT_DATASET_NOT_FOUND")
    csv_links = [
        href for href, label in candidates[0]["links"]
        if "file-download" in href and "CSV" in label.upper()
    ]
    if len(csv_links) != 1:
        raise ValueError("INTERNATIONAL_ESTAT_CSV_LINK_MISSING")
    href = csv_links[0]
    match = re.fullmatch(r"/stat-search/file-download\?statInfId=(\d+)&fileKind=1", href)
    if not match:
        raise ValueError("INTERNATIONAL_ESTAT_CSV_LINK_REJECTED")
    return "https://www.e-stat.go.jp" + href


def _parse_japan_core_csv(body, reference_period: str) -> str:
    if isinstance(body, bytes):
        try:
            text = body.decode("cp932", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("INTERNATIONAL_SOURCE_ENCODING_INVALID") from exc
    else:
        text = body
    try:
        rows = list(csv_reader(io.StringIO(text)))
    except Exception as exc:
        raise ValueError("INTERNATIONAL_ESTAT_CSV_INVALID") from exc
    if len(rows) < 7 or len(rows[1]) != len(rows[2]):
        raise ValueError("INTERNATIONAL_ESTAT_CSV_INVALID")
    headings = [re.sub(r"\s+", " ", value).strip() for value in rows[1]]
    target_columns = [index for index, value in enumerate(headings) if value == "All items, less fresh food"]
    if len(target_columns) != 1:
        raise ValueError("INTERNATIONAL_ESTAT_CORE_SERIES_MISSING")
    expected_row = reference_period.replace("-", "")
    matches = [row for row in rows if row and row[0].strip() == expected_row]
    if len(matches) != 1 or len(matches[0]) <= target_columns[0]:
        raise ValueError("INTERNATIONAL_REFERENCE_PERIOD_MISMATCH")
    value = matches[0][target_columns[0]].strip()
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
        raise ValueError("INTERNATIONAL_VALUE_INVALID")
    return value


def _find_official_article(index_body, *, host: str, predicate, error: str) -> str:
    doc = _document(index_body)
    links = []
    for href, label in doc.links:
        try:
            absolute = urljoin(f"https://{host}/", href)
            _validate_official_url(absolute)
        except ValueError:
            continue
        if predicate(label, absolute):
            links.append(absolute)
    links = list(dict.fromkeys(links))
    if len(links) != 1:
        raise ValueError(error)
    return links[0]


def _parse_swiss_cpi(text: str, reference_period: str) -> str:
    full_month, _short, year, _month = _month_names(reference_period)
    period = rf"{re.escape(full_month)}\s+{year}"
    if "Federal Statistical Office (FSO)" not in text:
        raise ValueError("INTERNATIONAL_SWISS_PUBLISHER_MISMATCH")
    pattern = rf"Consumer Price Index\s*\(CPI\)\s+(?P<movement>remained\s+(?:unchanged|stable)|(?:increased|rose|decreased|fell|declined)(?:\s+by)?\s+[-+]?\d+(?:\.\d+)?\s*%)\s+in\s+{period}\s+compared\s+with\s+the\s+previous\s+month"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        raise ValueError("INTERNATIONAL_CPI_SECTION_MISSING")
    movement = match.group("movement")
    if re.search(r"unchanged|stable", movement, flags=re.IGNORECASE):
        return "0.0"
    value_match = re.search(r"[-+]?\d+(?:\.\d+)?", movement)
    if value_match is None:
        raise ValueError("INTERNATIONAL_VALUE_INVALID")
    value = Decimal(value_match.group(0))
    if re.search(r"decreased|fell|declined", movement, flags=re.IGNORECASE):
        value = -abs(value)
    return _decimal_text(value)


def _parse_destatis_cpi(text: str, reference_period: str) -> tuple[str, bool]:
    period = _month_pattern(reference_period)
    match = re.search(
        rf"Consumer price index\s*,\s*{period}\s*:\s*(.*?)(?=Harmonised index of consumer prices|WIESBADEN)",
        text, flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("INTERNATIONAL_NATIONAL_CPI_SECTION_MISSING")
    section = match.group(1)
    value_match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%\s*on the previous month", section, flags=re.IGNORECASE)
    if value_match is None:
        raise ValueError("INTERNATIONAL_VALUE_INVALID")
    return value_match.group(1), bool(re.search(r"provisional", section, flags=re.IGNORECASE))


def _period_pair(reference_period: str) -> tuple[str, str]:
    current_year, current_month = (int(value) for value in reference_period.split("-"))
    prior = _month_shift(current_year, current_month, -1)
    py, pm = (int(value) for value in prior.split("-"))
    # ABS analytical-series headings use a full previous-month name and an
    # abbreviated reference-month name (for example, "July 26 to Aug 26").
    return date(py, pm, 1).strftime("%B %y"), date(current_year, current_month, 1).strftime("%b %y")


def _parse_abs_release(body, reference_period: str, metric: str) -> tuple[str, bool, datetime | None]:
    doc = _document(body)
    full_month, abbreviated_month, year, month = _month_names(reference_period)
    if not re.search(rf"Reference period\s+{re.escape(full_month)}\s+{year}", doc.text, flags=re.IGNORECASE):
        raise ValueError("INTERNATIONAL_REFERENCE_PERIOD_MISMATCH")
    tables = doc.tables
    release_table = None
    for table in tables:
        flat = " ".join(cell for row in table for cell in row)
        if "Original" in flat and "Seasonally adjusted" in flat and any(
            row and row[0].strip() == "All groups CPI" for row in table
        ):
            release_table = table
            break
    if release_table is None:
        raise ValueError("INTERNATIONAL_ABS_CPI_TABLE_MISSING")
    cpi_rows = [row for row in release_table if row and row[0].strip() == "All groups CPI"]
    if len(cpi_rows) != 1 or len(cpi_rows[0]) < 5:
        raise ValueError("INTERNATIONAL_ABS_CPI_ROW_MISSING")
    prior_month, current_month = _period_pair(reference_period)
    expected_month = f"{prior_month} to {current_month} (% change)"
    prior_year_period = _month_shift(year, month, -12)
    prior_year = int(prior_year_period[:4])
    expected_annual = f"{abbreviated_month} {prior_year % 100:02d} to {abbreviated_month} {year % 100:02d} (% change)"
    cpi_header = next((row for row in release_table if row and row[0].strip() == "Weighted average of eight capital cities"), None)
    if (cpi_header is None or len(cpi_header) < 5
            or cpi_header[1].strip() != expected_month
            or cpi_header[2].strip() != expected_annual
            or cpi_header[3].strip() != expected_month
            or cpi_header[4].strip() != expected_annual):
        raise ValueError("INTERNATIONAL_ABS_CPI_BASIS_MISMATCH")
    if metric == "abs_cpi_yoy":
        value = cpi_rows[0][2]
    elif metric == "abs_cpi_mom":
        value = cpi_rows[0][3]
    elif metric == "abs_trimmed_mean_mom":
        prior_short, current_short = _period_pair(reference_period)
        table = next((table for table in tables if table and table[0]
                      and table[0][0].strip() == "Analytical series"
                      and len(table[0]) >= 2
                      and table[0][1].strip() == f"{prior_short} to {current_short} (% change)"), None)
        if table is None:
            raise ValueError("INTERNATIONAL_ABS_TRIMMED_MEAN_TABLE_MISSING")
        rows = [row for row in table if row and row[0].strip() == "Trimmed mean"]
        if len(rows) != 1 or len(rows[0]) < 2:
            raise ValueError("INTERNATIONAL_ABS_TRIMMED_MEAN_ROW_MISSING")
        value = rows[0][1]
    else:
        raise ValueError("INTERNATIONAL_METRIC_UNMAPPED")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value.strip()):
        raise ValueError("INTERNATIONAL_VALUE_INVALID")
    return value.strip(), False, _parse_abs_publication_time(doc.text)


def _parse_abs_publication_time(text: str) -> datetime | None:
    match = re.search(
        r"Release date and time\s+(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*(am|pm)\s*AEST",
        text, flags=re.IGNORECASE,
    )
    if not match:
        return None
    day, month, year, hour, minute = (int(match.group(i)) for i in range(1, 6))
    hour = hour % 12 + (12 if match.group(6).lower() == "pm" else 0)
    local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Australia/Sydney"))
    return local.astimezone(UTC)


def _parse_rba_cash_rate(text: str) -> str:
    match = re.search(
        r"cash rate target(?:\s+by\s+\d+(?:\.\d+)?\s+basis points)?\s+(?:to|at)\s+([0-9]+(?:\.[0-9]+)?)\s+per\s+cent",
        text, flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("INTERNATIONAL_RBA_RATE_MISSING")
    return match.group(1)


def _parse_statcan_gdp(text: str, reference_period: str) -> tuple[str, bool]:
    full_month, _short, year, _month = _month_names(reference_period)
    pattern = rf"Real GDP by industry\s+{re.escape(full_month)}\s+{year}\s+([-+]?\d+(?:\.\d+)?)\s*%\s*\(monthly change\)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        raise ValueError("INTERNATIONAL_STATCAN_GDP_SECTION_MISSING")
    return match.group(1), bool(re.search(rf"{re.escape(full_month)}\s+{year}\s*p\b", text, flags=re.IGNORECASE))


def _decimal_text(value: Decimal) -> str:
    result = format(value.normalize(), "f")
    return "0" if result in {"-0", ""} else result


def _value_display(value: str, *, percent=True) -> str:
    value = str(value).strip()
    try:
        dec = Decimal(value.replace("−", "-").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("INTERNATIONAL_VALUE_INVALID") from exc
    if not dec.is_finite():
        raise ValueError("INTERNATIONAL_VALUE_INVALID")
    if dec == 0:
        value = "0.0" if "." in value else "0"
    else:
        value = value.replace("−", "-").replace("+", "")
    return f"{value}%" if percent else value


def _result(metric: str, event: dict, reference_period: str, value: str, *, fetcher: _BoundFetcher,
            source_url: str, data_url: str | None, published_at: datetime,
            published_at_source: str, estimate: bool, basis: str | None = None,
            methodology_url: str | None = None) -> dict:
    available_at = max(fetcher.completions) if fetcher.completions else None
    if available_at is None:
        raise ValueError("INTERNATIONAL_SOURCE_FETCH_FAILED:NoFetchTimestamp")
    as_of = _utc(fetcher.clock())
    published_at = _utc(published_at)
    if published_at > as_of or available_at > as_of:
        raise ValueError("INTERNATIONAL_SOURCE_NOT_YET_KNOWN_AS_OF")
    if available_at < published_at:
        raise ValueError("INTERNATIONAL_FETCH_PRECEDES_RELEASE")
    try:
        value_decimal = Decimal(str(value).replace("−", "-").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("INTERNATIONAL_VALUE_INVALID") from exc
    if not value_decimal.is_finite():
        raise ValueError("INTERNATIONAL_VALUE_INVALID")
    spec = _EVENT_SPECS[metric]
    display = _value_display(value)
    actual_basis = basis or {
        "jpy_tokyo_core_cpi_yoy": "year_over_year_not_seasonally_adjusted",
        "chf_cpi_mom": "month_over_month_not_seasonally_adjusted",
        "destatis_german_cpi_mom": "month_over_month_not_seasonally_adjusted",
        "abs_cpi_yoy": "year_over_year_not_seasonally_adjusted",
        "abs_cpi_mom": "seasonally_adjusted_month_over_month",
        "abs_trimmed_mean_mom": "seasonally_adjusted_month_over_month",
        "rba_cash_rate": "cash_rate_target",
        "statcan_gdp_mom": "seasonally_adjusted_month_over_month_chained_2017_dollars",
    }[metric]
    return {
        "actual": display,
        "actual_display": display,
        "actual_value": _decimal_text(value_decimal),
        "actual_unit": "percent_per_annum" if metric == "rba_cash_rate" else "percent",
        "actual_basis": actual_basis,
        "actual_method": "OFFICIAL_REPORTED",
        "actual_is_estimate": bool(estimate),
        "actual_reference_period": reference_period,
        "actual_published_at": _iso(published_at),
        "actual_published_at_source": published_at_source,
        "actual_available_at": _iso(available_at),
        "actual_fetched_at": _iso(available_at),
        "actual_provider": spec["provider"],
        "actual_source_url": source_url,
        "actual_data_url": data_url,
        "actual_methodology_url": methodology_url,
        "actual_status": "VERIFIED",
    }


def _fetch_swiss_actual(event, metric, reference_period, fetcher, event_time, publication_date):
    index, _ = fetcher(SWISS_NEWS_INDEX_URL)
    full_month, _short, _year, _month = _month_names(reference_period)
    article_url = _find_official_article(
        index, host="www.efd.admin.ch",
        predicate=lambda label, _url: "consumer prices" in label.lower() and full_month.lower() in label.lower(),
        error="INTERNATIONAL_SWISS_RELEASE_NOT_FOUND",
    )
    body, _ = fetcher(article_url)
    text = _document(body).text
    _date_match(text, publication_date, "swiss")
    value = _parse_swiss_cpi(text, reference_period)
    if not re.search(rf"Consumer prices .*?{re.escape(full_month)}", text, flags=re.IGNORECASE):
        raise ValueError("INTERNATIONAL_REFERENCE_PERIOD_MISMATCH")
    return _result(metric, event, reference_period, value, fetcher=fetcher, source_url=article_url,
                   data_url=None, published_at=event_time,
                   published_at_source="official_release_date_and_scheduled_release_time", estimate=False)


def _fetch_destatis_actual(event, metric, reference_period, fetcher, event_time, publication_date):
    index, _ = fetcher(DESTATIS_PRESS_INDEX_URL)
    full_month, _short, year, _month = _month_names(reference_period)
    article_url = _find_official_article(
        index, host="www.destatis.de",
        predicate=lambda label, _url: "inflation rate" in label.lower()
        and bool(re.search(rf"\bin\s+{re.escape(full_month)}\s+{year}\b", label, flags=re.IGNORECASE))
        and "cpi" not in label.lower(),
        error="INTERNATIONAL_DESTATIS_RELEASE_NOT_FOUND",
    )
    body, _ = fetcher(article_url)
    text = _document(body).text
    _date_match(text, publication_date, "destatis")
    value, estimate = _parse_destatis_cpi(text, reference_period)
    return _result(metric, event, reference_period, value, fetcher=fetcher, source_url=article_url,
                   data_url=None, published_at=event_time,
                   published_at_source="official_release_date_and_scheduled_release_time", estimate=estimate)


def _fetch_japan_actual(event, metric, reference_period, fetcher, event_time, publication_date):
    release_body, _ = fetcher(JAPAN_RELEASE_URL)
    release_text = _document(release_body, encoding="shift_jis").text
    period_pattern = rf"{reference_period[:4]}\s*年(?:\s*（令和\s*\d+\s*年）)?\s*{int(reference_period[5:])}\s*月分"
    if not re.search(r"東京都区部", release_text) or not re.search(period_pattern, release_text):
        raise ValueError("INTERNATIONAL_REFERENCE_PERIOD_MISMATCH")
    _date_match(release_text, publication_date, "japan")

    dataset_page, _ = fetcher(JAPAN_ESTAT_LIST_URL)
    csv_url = _resolve_estat_core_csv(dataset_page, reference_period, publication_date)
    csv_body, _ = fetcher(csv_url)
    value = _parse_japan_core_csv(csv_body, reference_period)
    return _result(metric, event, reference_period, value, fetcher=fetcher,
                   source_url=JAPAN_RELEASE_URL, data_url=csv_url, published_at=event_time,
                   published_at_source="official_release_date_and_scheduled_release_time", estimate=True,
                   basis="year_over_year_not_seasonally_adjusted")


def _fetch_abs_actual(event, metric, reference_period, fetcher, event_time, publication_date):
    body, _ = fetcher(ABS_LATEST_RELEASE_URL)
    doc = _document(body)
    _date_match(doc.text, publication_date, "abs")
    full_month, _short, year, _month = _month_names(reference_period)
    if not re.search(rf"Consumer Price Index, Australia\s*,?\s*{re.escape(full_month)}\s+{year}", doc.text, flags=re.IGNORECASE):
        raise ValueError("INTERNATIONAL_REFERENCE_PERIOD_MISMATCH")
    value, estimate, published = _parse_abs_release(body, reference_period, metric)
    published_at = published or event_time
    if published_at.date() != event_time.date():
        raise ValueError("INTERNATIONAL_RELEASE_DATE_MISMATCH")
    basis = {
        "abs_cpi_yoy": "year_over_year_not_seasonally_adjusted",
        "abs_cpi_mom": "seasonally_adjusted_month_over_month",
        "abs_trimmed_mean_mom": "seasonally_adjusted_month_over_month",
    }[metric]
    methodology_url = None
    if metric == "abs_trimmed_mean_mom":
        methodology_url = ABS_TRIMMED_MEAN_METHODOLOGY_URL
        methodology_body, _ = fetcher(methodology_url)
        methodology_text = _document(methodology_body).text
        if not re.search(
            r"monthly\s+trimmed\s+mean\s+series\s+is\s+calculated\s+from\s+seasonally\s+adjusted\s+series",
            methodology_text, flags=re.IGNORECASE,
        ):
            raise ValueError("INTERNATIONAL_ABS_TRIMMED_MEAN_BASIS_UNVERIFIED")
    return _result(metric, event, reference_period, value, fetcher=fetcher,
                   source_url=ABS_LATEST_RELEASE_URL, data_url=ABS_LATEST_RELEASE_URL,
                   published_at=published_at, published_at_source="official_release_page_release_date_and_time",
                   estimate=estimate, basis=basis, methodology_url=methodology_url)


def _fetch_rba_actual(event, metric, reference_period, fetcher, event_time, publication_date):
    index, _ = fetcher(RBA_DECISIONS_INDEX_URL)
    # Build the local-date label portably across Windows and POSIX.
    rendered_date = f"{publication_date.day} {publication_date.strftime('%B %Y')}"
    release_url = _find_official_article(
        index, host="www.rba.gov.au",
        predicate=lambda label, _url: label.strip() == rendered_date,
        error="INTERNATIONAL_RBA_DECISION_NOT_FOUND",
    )
    body, _ = fetcher(release_url)
    text = _document(body).text
    _date_match(text, publication_date, "rba")
    value = _parse_rba_cash_rate(text)
    return _result(metric, event, reference_period, value, fetcher=fetcher,
                   source_url=release_url, data_url=None, published_at=event_time,
                   published_at_source="official_decision_date_and_scheduled_release_time", estimate=False,
                   basis="cash_rate_target")


def _fetch_statcan_actual(event, metric, reference_period, fetcher, event_time, publication_date):
    release_url = f"{STATCAN_RELEASE_ROOT}{publication_date:%y%m%d}/dq{publication_date:%y%m%d}a-eng.htm"
    _validate_official_url(release_url)
    body, _ = fetcher(release_url)
    text = _document(body).text
    _date_match(text, publication_date, "statcan")
    full_month, _short, year, _month = _month_names(reference_period)
    if not re.search(rf"Gross domestic product by industry,?\s+{re.escape(full_month)}\s+{year}", text, flags=re.IGNORECASE):
        raise ValueError("INTERNATIONAL_REFERENCE_PERIOD_MISMATCH")
    value, estimate = _parse_statcan_gdp(text, reference_period)
    return _result(metric, event, reference_period, value, fetcher=fetcher,
                   source_url=release_url, data_url=release_url, published_at=event_time,
                   published_at_source="official_release_date_and_scheduled_release_time", estimate=estimate)


def fetch_international_actual(event: dict, *, fetch=None, now=None, clock=None) -> dict:
    """Fetch and validate one exact official release, returning macro actual fields.

    An injected fetcher receives only module-owned official HTTPS URLs and may
    return a body or ``(body, completed_at)``. No event field can select a host
    or URL. ``now`` sets the as-of ceiling; ``clock`` timestamps injected reads.
    """
    metric = map_international_event(event)
    if metric is None:
        raise ValueError("INTERNATIONAL_METRIC_UNMAPPED")
    event_time = _utc(event.get("event_time"))
    now_value = now() if callable(now) else now
    supplied_now = now_value is not None
    as_of = _utc(now_value if supplied_now else datetime.now(UTC))
    if event_time > as_of:
        raise ValueError("INTERNATIONAL_RELEASE_NOT_YET_AVAILABLE")
    # A fixed explicit as-of remains fixed unless a caller deliberately supplies
    # a ticking clock to timestamp and bound the completed reads.
    clock_fn = clock or ((lambda: as_of) if supplied_now else (lambda: datetime.now(UTC)))
    reference_period = international_reference_period(metric, event_time)
    publication_date = _event_publication_date(event_time, metric)
    fetcher = _BoundFetcher(fetch, clock_fn)
    if metric == "jpy_tokyo_core_cpi_yoy":
        return _fetch_japan_actual(event, metric, reference_period, fetcher, event_time, publication_date)
    if metric == "chf_cpi_mom":
        return _fetch_swiss_actual(event, metric, reference_period, fetcher, event_time, publication_date)
    if metric == "destatis_german_cpi_mom":
        return _fetch_destatis_actual(event, metric, reference_period, fetcher, event_time, publication_date)
    if metric.startswith("abs_"):
        return _fetch_abs_actual(event, metric, reference_period, fetcher, event_time, publication_date)
    if metric == "rba_cash_rate":
        return _fetch_rba_actual(event, metric, reference_period, fetcher, event_time, publication_date)
    if metric == "statcan_gdp_mom":
        return _fetch_statcan_actual(event, metric, reference_period, fetcher, event_time, publication_date)
    raise ValueError("INTERNATIONAL_METRIC_UNMAPPED")


def fetch_rba_text(event: dict, *, fetch=None, now=None, clock=None) -> dict:
    """Fetch the RBA's date-matched decision statement and press conference text.

    This is qualitative evidence only. It deliberately returns no numeric
    trading signal, macro surprise, or direction.
    """
    if map_international_event(event) != "rba_cash_rate" and not _is_rba_text_event(event):
        raise ValueError("INTERNATIONAL_RBA_EVENT_UNMAPPED")
    event_time = _utc(event.get("event_time"))
    now_value = now() if callable(now) else now
    supplied_now = now_value is not None
    as_of = _utc(now_value if supplied_now else datetime.now(UTC))
    if event_time > as_of:
        raise ValueError("INTERNATIONAL_RELEASE_NOT_YET_AVAILABLE")
    clock_fn = clock or ((lambda: as_of) if supplied_now else (lambda: datetime.now(UTC)))
    fetcher = _BoundFetcher(fetch, clock_fn)
    local_date = event_time.astimezone(ZoneInfo("Australia/Sydney")).date()
    index, _ = fetcher(RBA_DECISIONS_INDEX_URL)
    rendered_date = f"{local_date.day} {local_date.strftime('%B %Y')}"
    release_url = _find_official_article(
        index, host="www.rba.gov.au", predicate=lambda label, _url: label.strip() == rendered_date,
        error="INTERNATIONAL_RBA_DECISION_NOT_FOUND",
    )
    release_body, _ = fetcher(release_url)
    release = _document(release_body)
    conference_url = (
        f"https://www.rba.gov.au/speeches/{local_date.year}/mc-gov-"
        f"{local_date.year}-{local_date.month:02d}-{local_date.day:02d}.html"
    )
    conference_body, _ = fetcher(conference_url)
    conference = _document(conference_body)
    _date_match(release.text, local_date, "rba")
    if f"{local_date.day}\u00a0{local_date.strftime('%B %Y')}" not in conference.text and not re.search(
        rf"{local_date.day}\s+{local_date.strftime('%B %Y')}", conference.text
    ):
        raise ValueError("INTERNATIONAL_RBA_CONFERENCE_DATE_MISMATCH")
    statement_title = "Statement by the Monetary Policy Board: Monetary Policy Decision"
    if statement_title.lower() not in release.text.lower():
        raise ValueError("INTERNATIONAL_RBA_STATEMENT_MISSING")
    if "Media Conference" not in conference.text or "Monetary Policy Decision" not in conference.text:
        raise ValueError("INTERNATIONAL_RBA_CONFERENCE_MISSING")
    # Preserve dated official text snippets in source order; cap the copied
    # qualitative payload to keep downstream contexts compact.
    release_start = release.text.lower().find("at its meeting today")
    conference_start = conference.text.lower().find("good afternoon")
    if release_start < 0 or conference_start < 0:
        raise ValueError("INTERNATIONAL_RBA_TEXT_MISSING")
    release_excerpt = release.text[release_start:release_start + 250]
    conference_excerpt = conference.text[conference_start:conference_start + 250]
    available_at = max(fetcher.completions)
    final_as_of = _utc(clock_fn())
    if available_at > final_as_of:
        raise ValueError("INTERNATIONAL_SOURCE_NOT_YET_KNOWN_AS_OF")
    return {
        "qualitative_source_url": release_url,
        "qualitative_conference_url": conference_url,
        "qualitative_provider": "Reserve Bank of Australia (RBA)",
        "qualitative_title": "Monetary Policy Decision and Media Conference",
        "qualitative_summary": f"RBA decision statement: {release_excerpt}\nRBA press conference: {conference_excerpt}",
        "qualitative_published_at": _iso(event_time),
        "qualitative_available_at": _iso(available_at),
        "qualitative_status": "OFFICIAL_TEXT_AVAILABLE",
    }
