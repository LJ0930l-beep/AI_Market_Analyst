"""Strict, credential-free readers for selected dated U.S. macro releases.

The event calendar is only a schedule.  This module accepts an event only when
its exact title and currency are known, follows a small set of official pages,
and binds every parsed number to the expected publication period.
"""

from __future__ import annotations

import re
import zlib
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .macro_actuals import iso_utc, utc_datetime

MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 12
MAX_REQUESTS_PER_RELEASE = 3
MAX_PDF_STREAMS = 64
MAX_PDF_DECOMPRESSED_BYTES = 12_000_000
MAX_PDF_TEXT_ARRAY_BYTES = 65_536
_EASTERN = ZoneInfo("America/New_York")

BEA_CURRENT_RELEASES = "https://www.bea.gov/news/current-releases"
BEA_GDP_PRICE_INDEX = "https://www.bea.gov/data/prices-inflation/gdp-price-index"
ISM_REPORT_CALENDAR = "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/"
ISM_REPORTS_INDEX = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/"
ISM_PRNEWSWIRE_CHANNEL = "https://www.prnewswire.com/news/institute-for-supply-management/"
ADP_RELEASES_INDEX = "https://mediacenter.adp.com/press-releases"
CB_CONFIDENCE_PAGE = "https://www.conference-board.org/topics/consumer-confidence/"
DOL_WEEKLY_CLAIMS_PDF = "https://www.dol.gov/ui/data.pdf"

_TITLE_METRICS = {
    "final gdp q/q": "bea_final_gdp_qoq",
    "final gdp price index q/q": "bea_final_gdp_price_index_qoq",
    "core pce price index m/m": "bea_core_pce_mom",
    "unemployment claims": "dol_unemployment_claims",
    "ism manufacturing pmi": "ism_manufacturing_pmi",
    "adp non-farm employment change": "adp_nonfarm_employment_change",
    "cb consumer confidence": "conference_board_consumer_confidence",
    "jolts job openings": "bls_jolts_job_openings",
    "job openings and labor turnover survey": "bls_jolts_job_openings",
}
_PROVIDER = {
    "bea_final_gdp_qoq": "BEA",
    "bea_final_gdp_price_index_qoq": "BEA",
    "bea_core_pce_mom": "BEA",
    "dol_unemployment_claims": "U.S. Department of Labor",
    "ism_manufacturing_pmi": "Institute for Supply Management",
    "adp_nonfarm_employment_change": "ADP Research",
    "conference_board_consumer_confidence": "The Conference Board",
    "bls_jolts_job_openings": "BLS",
}


def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _event_datetime(event: dict) -> datetime:
    if not isinstance(event, dict):
        raise ValueError("RELEASE_EVENT_INVALID")
    try:
        return utc_datetime(event.get("event_time"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("RELEASE_EVENT_TIME_INVALID") from exc


def map_release_event(event: dict) -> str | None:
    """Map only exact supported USD calendar labels to an official metric."""
    if not isinstance(event, dict):
        return None
    title = _clean_text(event.get("title")).casefold()
    currency = re.sub(r"[^a-z]", "", _clean_text(event.get("currency")).casefold())
    if currency not in {"usd", "us", "unitedstates", "unitedstatesofamerica"}:
        return None
    return _TITLE_METRICS.get(title)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + delta
    return absolute // 12, absolute % 12 + 1


def _expected_period(metric: str, at: datetime) -> str:
    local = at.astimezone(_EASTERN)
    if metric in {"bea_final_gdp_qoq", "bea_final_gdp_price_index_qoq"}:
        quarter = (local.month - 1) // 3 + 1
        year = local.year
        quarter -= 1
        if quarter == 0:
            year -= 1
            quarter = 4
        return f"{year}-Q{quarter}"
    if metric == "dol_unemployment_claims":
        release_day = local.date()
        # Claims releases report the week ending on the Saturday before release.
        return (release_day - timedelta(days=(release_day.weekday() + 2) % 7)).isoformat()
    year, month = (local.year, local.month)
    if metric in {
        "bea_core_pce_mom", "ism_manufacturing_pmi", "bls_jolts_job_openings",
    }:
        year, month = _shift_month(year, month, -1)
    return f"{year:04d}-{month:02d}"


def _event_local_day(event: dict) -> date:
    return _event_datetime(event).astimezone(_EASTERN).date()


def _release_date_time(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), _EASTERN).astimezone(_EASTERN)


def _allowed_source_url(url: str) -> bool:
    """Return true only for known official index/data endpoints used here."""
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        return False
    host, path = (parsed.hostname or "").lower(), parsed.path
    if parsed.query or parsed.fragment:
        return False
    if host in {"bea.gov", "www.bea.gov"}:
        return (
            path == "/news/current-releases"
            or path == "/data/prices-inflation/gdp-price-index"
            or bool(re.fullmatch(r"/news/\d{4}/gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-(?:1st|2nd|3rd|4th)", path))
            or bool(re.fullmatch(r"/news/\d{4}/personal-income-and-outlays-[a-z]+-\d{4}", path))
        )
    if host == "www.dol.gov":
        return path == "/ui/data.pdf" or bool(re.fullmatch(r"/newsroom/releases/eta/eta\d{8}", path))
    if host == "www.bls.gov":
        return bool(re.fullmatch(r"/news\.release/archives/jolts_\d{8}\.htm", path))
    if host == "www.ismworld.org":
        return (
            path == "/supply-management-news-and-reports/reports/rob-report-calendar/"
            or path == "/supply-management-news-and-reports/reports/ism-pmi-reports/"
            or bool(re.fullmatch(r"/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/[a-z]+/", path))
        )
    if host == "www.prnewswire.com":
        return path == "/news/institute-for-supply-management/" or bool(re.fullmatch(
            r"/news-releases/manufacturing-pmi-at-\d+(?:-\d+)?-"
            r"(?:january|february|march|april|may|june|july|august|september|october|november|december)-"
            r"20\d{2}-ism-manufacturing-pmi-report-\d+\.html",
            path,
        ))
    if host == "mediacenter.adp.com":
        return path == "/press-releases" or bool(re.fullmatch(
            r"/\d{4}-\d{2}-\d{2}-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-[A-Za-z0-9,%-]+-Jobs-in-[A-Za-z]+", path
        ))
    if host == "www.conference-board.org":
        return path == "/topics/consumer-confidence/"
    return False


def _url(url: str, *, exact: str | None = None) -> str:
    if not _allowed_source_url(url) or (exact is not None and url != exact):
        raise ValueError("OFFICIAL_SOURCE_URL_REJECTED")
    return url


class _Document(HTMLParser):
    """Small stdlib parser retaining visible text, anchors, and table rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.tables: list[list[list[str]]] = []
        self._href: str | None = None
        self._anchor: list[str] | None = None
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag == "a":
            self._href = attrs.get("href")
            self._anchor = []
        elif tag == "table" and self._table is None:
            self._table = []
        elif tag == "tr" and self._table is not None and self._row is None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None and self._cell is None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self._cell is not None:
            self._row.append(_clean_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "a" and self._anchor is not None:
            self.links.append((self._href or "", _clean_text("".join(self._anchor))))
            self._href = None
            self._anchor = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._anchor is not None:
            self._anchor.append(data)
        cleaned = _clean_text(data)
        if cleaned:
            self.text_parts.append(cleaned)

    @property
    def text(self) -> str:
        return _clean_text(" ".join(self.text_parts))


class _FetchSession:
    def __init__(self, fetch, clock, now):
        self.fetch = fetch
        self.clock = clock
        self.now = now
        self.count = 0
        self.completed_at: datetime | None = None

    def get(self, url: str, *, exact: str | None = None):
        _url(url, exact=exact)
        self.count += 1
        if self.count > MAX_REQUESTS_PER_RELEASE:
            raise ValueError("OFFICIAL_REQUEST_LIMIT_EXCEEDED")
        if self.fetch is not None:
            result = self.fetch(url)
            if isinstance(result, tuple) and len(result) == 2:
                body, completed = result
                completed_at = utc_datetime(completed)
            else:
                body = result
                completed_at = utc_datetime(self.clock())
        else:
            request = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; AI-Market-Analyst/2.0; public macro actual reader)",
                "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5",
            })
            try:
                opener = build_opener(_OfficialRedirectHandler())
                with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    final_url = response.geturl()
                    if final_url != url or not _allowed_source_url(final_url):
                        raise ValueError("OFFICIAL_SOURCE_REDIRECT_REJECTED")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except HTTPError as exc:
                raise OSError(f"OFFICIAL_HTTP_{exc.code}:{urlparse(url).hostname}") from exc
            except URLError as exc:
                raise OSError(f"OFFICIAL_NETWORK_ERROR:{urlparse(url).hostname}:{type(exc.reason).__name__}") from exc
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("OFFICIAL_RESPONSE_TOO_LARGE")
            body = raw
            completed_at = utc_datetime(self.clock())
        if isinstance(body, str):
            encoded_size = len(body.encode("utf-8"))
        elif isinstance(body, (bytes, bytearray)):
            encoded_size = len(body)
        else:
            raise ValueError("OFFICIAL_RESPONSE_TYPE_INVALID")
        if encoded_size > MAX_RESPONSE_BYTES:
            raise ValueError("OFFICIAL_RESPONSE_TOO_LARGE")
        if completed_at > utc_datetime(self.clock()):
            raise ValueError("OFFICIAL_FETCH_TIMESTAMP_IN_FUTURE")
        self.completed_at = max(self.completed_at or completed_at, completed_at)
        return body


class _OfficialRedirectHandler(HTTPRedirectHandler):
    """Do not follow an HTTP redirect unless its destination is allowlisted."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_source_url(newurl):
            raise ValueError("OFFICIAL_SOURCE_REDIRECT_REJECTED")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _decode(body) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body


def _parse_decimal(raw: str) -> str:
    normalized = _clean_text(raw).replace(",", "").replace("%", "").replace("+", "")
    try:
        value = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("OFFICIAL_VALUE_INVALID") from exc
    if not value.is_finite():
        raise ValueError("OFFICIAL_VALUE_INVALID")
    rendered = format(value.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _quarter_bounds(period: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})-Q([1-4])", period)
    if not match:
        raise ValueError("OFFICIAL_PERIOD_INVALID")
    return int(match.group(1)), int(match.group(2))


def _quarter_words(period: str) -> tuple[str, str, str]:
    year, quarter = _quarter_bounds(period)
    words = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
    return str(year), str(quarter), words[quarter]


def _month_name(period: str) -> str:
    year, month = (int(piece) for piece in period.split("-"))
    return date(year, month, 1).strftime("%B")


def _find_bea_release(session: _FetchSession, metric: str, period: str) -> tuple[str, str]:
    index = _Document()
    index.feed(_decode(session.get(BEA_CURRENT_RELEASES)))
    year, _quarter, ordinal = _quarter_words(period) if metric != "bea_core_pce_mom" else ("", "", "")
    month_name = _month_name(period) if metric == "bea_core_pce_mom" else ""
    for href, label in index.links:
        label_low = label.casefold()
        if metric in {"bea_final_gdp_qoq", "bea_final_gdp_price_index_qoq"}:
            if "gdp (third estimate)" not in label_low or f"{ordinal} quarter {year}" not in label_low:
                continue
            expected_piece = f"gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-{ordinal}"
            path = urlparse(href).path if href.startswith("http") else href
            if expected_piece not in path:
                continue
        else:
            if not label_low.startswith("personal income and outlays,") or f"{month_name} {period[:4]}" not in label:
                continue
            expected_slug = f"personal-income-and-outlays-{month_name.lower()}-{period[:4]}"
            path = urlparse(href).path if href.startswith("http") else href
            if not path.endswith("/" + expected_slug):
                continue
        if href.startswith("/"):
            href = "https://www.bea.gov" + href
        _url(href)
        page = _Document()
        page.feed(_decode(session.get(href)))
        expected = (f"{ordinal} quarter {year}" if metric != "bea_core_pce_mom" else f"{month_name} {period[:4]}")
        if expected.casefold() not in page.text.casefold():
            raise ValueError("OFFICIAL_PERIOD_MISMATCH:BEA_RELEASE_BODY")
        return href, page.text
    raise ValueError("OFFICIAL_RELEASE_NOT_FOUND:BEA_CURRENT_RELEASE_INDEX")


def _extract_bea(metric: str, period: str, article_text: str, session: _FetchSession) -> tuple[str, str]:
    if metric == "bea_final_gdp_qoq":
        year, quarter = _quarter_bounds(period)
        month_range = {1: "January, February, and March", 2: "April, May, and June",
                       3: "July, August, and September", 4: "October, November, and December"}[quarter]
        pattern = re.compile(
            rf"Real gross domestic product \(GDP\) increased at an annual rate of\s*([+-]?\d+(?:\.\d+)?)\s*percent\s+"
            rf"in the {re.escape({1:'first',2:'second',3:'third',4:'fourth'}[quarter])} quarter of {year}"
        , re.IGNORECASE)
        match = pattern.search(article_text)
        if not match or month_range.casefold() not in article_text.casefold():
            raise ValueError("OFFICIAL_PERIOD_MISMATCH:BEA_GDP_QUARTER")
        return _parse_decimal(match.group(1)), "percent; quarter-over-quarter annualized"
    if metric == "bea_final_gdp_price_index_qoq":
        html = _decode(session.get(BEA_GDP_PRICE_INDEX))
        doc = _Document()
        doc.feed(html)
        target = f"Q{_quarter_bounds(period)[1]} {period[:4]} (3rd)"
        if "quarterly - percent change from preceding quarter" not in doc.text.casefold():
            raise ValueError("OFFICIAL_VALUE_NOT_FOUND:BEA_GDP_PRICE_TABLE")
        for table in doc.tables:
            for row in table:
                if len(row) >= 2 and row[0].casefold() == target.casefold():
                    return _parse_decimal(row[1]), "percent; quarter-over-quarter"
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:BEA_GDP_PRICE_INDEX")
    # _find_bea_release has already bound this article's exact title to the
    # expected reference month; the release sentence itself gives the monthly
    # core PCE rate.
    match = re.search(
        r"(?:PCE price index excluding food and energy|Excluding food and energy, the PCE price index)"
        r" increased\s*([+-]?\d+(?:\.\d+)?)\s*percent", article_text, re.IGNORECASE
    )
    if not match:
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:BEA_CORE_PCE_MONTH")
    return _parse_decimal(match.group(1)), "percent; month-over-month"


def _release_time_from_bea(text: str) -> datetime:
    match = re.search(
        r"EMBARGOED UNTIL RELEASE AT\s+(\d{1,2}:\d{2})\s*(a\.m\.|p\.m\.)\s*EDT,\s*"
        r"([A-Za-z]+),\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text, re.IGNORECASE
    )
    if not match:
        raise ValueError("OFFICIAL_PUBLISHED_AT_NOT_FOUND:BEA_EMBARGO")
    hour, minute = (int(part) for part in match.group(1).split(":"))
    if match.group(2).casefold().startswith("p") and hour != 12:
        hour += 12
    if match.group(2).casefold().startswith("a") and hour == 12:
        hour = 0
    day = date(int(match.group(6)), datetime.strptime(match.group(4), "%B").month, int(match.group(5)))
    return datetime.combine(day, time(hour, minute), _EASTERN).astimezone(ZoneInfo("UTC"))


def _parse_pdf_text(raw: bytes) -> str:
    """Extract simple text strings from DOL's tagged PDF content streams."""
    chunks = []
    stream_count = 0
    decompressed_total = 0
    for match in re.finditer(rb"stream\r?\n", raw):
        end = raw.find(b"endstream", match.end())
        if end < 0:
            continue
        stream = raw[match.end():end].rstrip(b"\r\n")
        try:
            decoder = zlib.decompressobj()
            remaining = MAX_PDF_DECOMPRESSED_BYTES - decompressed_total
            decoded_bytes = decoder.decompress(stream, remaining + 1)
            if len(decoded_bytes) > remaining or decoder.unconsumed_tail:
                raise ValueError("OFFICIAL_DOL_PDF_DECOMPRESSION_LIMIT")
            extra = decoder.flush(remaining - len(decoded_bytes) + 1)
            decoded_bytes += extra
            if len(decoded_bytes) > remaining:
                raise ValueError("OFFICIAL_DOL_PDF_DECOMPRESSION_LIMIT")
        except zlib.error:
            continue
        stream_count += 1
        if stream_count > MAX_PDF_STREAMS:
            raise ValueError("OFFICIAL_DOL_PDF_STREAM_LIMIT")
        decompressed_total += len(decoded_bytes)
        decoded = decoded_bytes.decode("latin-1", errors="ignore")

        # Scan every candidate array once. Avoid nested regular expressions on
        # compressed PDF content: a malformed huge literal can otherwise cause
        # catastrophic backtracking before the byte limit is noticed.
        cursor = 0
        while cursor < len(decoded):
            start = decoded.find("[", cursor)
            if start < 0:
                break
            pos = start + 1
            depth = 0
            in_string = False
            escaped = False
            close = -1
            limit = min(len(decoded), start + MAX_PDF_TEXT_ARRAY_BYTES + 1)
            while pos < limit:
                char = decoded[pos]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                        if depth <= 0:
                            in_string = False
                elif char == "(":
                    in_string = True
                    depth = 1
                elif char == "]":
                    close = pos
                    break
                pos += 1
            if close < 0:
                # Consume the bounded search window once, so a hostile stream
                # containing many unmatched '[' bytes still runs in linear time.
                cursor = max(pos, start + MAX_PDF_TEXT_ARRAY_BYTES)
                continue
            after = close + 1
            while after < len(decoded) and decoded[after].isspace():
                after += 1
            if decoded.startswith("TJ", after):
                value_parts = []
                pos = start + 1
                while pos < close:
                    if decoded[pos] != "(":
                        pos += 1
                        continue
                    pos += 1
                    nesting = 1
                    part = []
                    while pos < close and nesting:
                        char = decoded[pos]
                        pos += 1
                        if char == "\\" and pos < close:
                            escaped_char = decoded[pos]
                            pos += 1
                            part.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(escaped_char, escaped_char))
                        elif char == "(":
                            nesting += 1
                            part.append(char)
                        elif char == ")":
                            nesting -= 1
                            if nesting:
                                part.append(char)
                        else:
                            part.append(char)
                    value_parts.append("".join(part))
                if value_parts:
                    chunks.append("".join(value_parts))
            cursor = close + 1
    return _clean_text(" ".join(chunks))


def _parse_dol_pdf(raw: bytes, expected_period: str, expected_release_day: date) -> tuple[str, datetime]:
    if not raw.startswith(b"%PDF-"):
        raise ValueError("OFFICIAL_DOL_PDF_INVALID")
    text = _parse_pdf_text(raw)
    release_match = re.search(
        r"EMBARGOED UNTIL\s+(\d{1,2}:\d{2})\s*A\.M\.\s*\(Eastern\)\s*\w+,\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
        text, re.IGNORECASE,
    )
    if not release_match:
        raise ValueError("OFFICIAL_PUBLISHED_AT_NOT_FOUND:DOL_PDF_COVER")
    month = datetime.strptime(release_match.group(2), "%B").month
    published_day = date(int(release_match.group(4)), month, int(release_match.group(3)))
    if published_day != expected_release_day:
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:DOL_RELEASE_DATE")
    hour, minute = (int(value) for value in release_match.group(1).split(":"))
    published = datetime.combine(published_day, time(hour, minute), _EASTERN).astimezone(ZoneInfo("UTC"))
    expected_week = datetime.strptime(expected_period, "%Y-%m-%d").strftime("%B %d").replace(" 0", " ")
    claims = re.search(
        rf"In the week ending\s+{re.escape(expected_week)},\s+the advance figure for seasonally adjusted initial claims\s+was\s*([\d,]+)",
        text, re.IGNORECASE,
    )
    if not claims:
        if not re.search(rf"In the week ending\s+{re.escape(expected_week)}\b", text, re.IGNORECASE):
            raise ValueError("OFFICIAL_PERIOD_MISMATCH:DOL_WEEK_ENDING")
        raise ValueError("OFFICIAL_VALUE_NOT_FOUND:DOL_INITIAL_CLAIMS")
    return _parse_decimal(claims.group(1)), published


def _dol_actual(session: _FetchSession, period: str, release_day: date) -> tuple[str, str, datetime]:
    raw = session.get(DOL_WEEKLY_CLAIMS_PDF, exact=DOL_WEEKLY_CLAIMS_PDF)
    value, published = _parse_dol_pdf(raw if isinstance(raw, bytes) else raw.encode("latin-1"), period, release_day)
    return value, DOL_WEEKLY_CLAIMS_PDF, published


def _month_slug(period: str) -> str:
    return _month_name(period).lower()


def _ism_value(text: str, period: str) -> str:
    month = _month_name(period)
    value_match = re.search(
        rf"Manufacturing PMI[^.\n]{{0,100}}?registered\s+([\d.]+)\s+percent\s+in\s+{month}",
        text, re.IGNORECASE,
    )
    if not value_match:
        value_match = re.search(r"Manufacturing PMI at\s*([\d.]+)%", text, re.IGNORECASE)
    if not value_match:
        raise ValueError("OFFICIAL_VALUE_NOT_FOUND:ISM_MANUFACTURING_PMI")
    return _parse_decimal(value_match.group(1))


def _ism_prnewswire_release(session: _FetchSession, period: str, release_day: date) -> tuple[str, str, datetime]:
    index = _Document()
    index.feed(_decode(session.get(ISM_PRNEWSWIRE_CHANNEL, exact=ISM_PRNEWSWIRE_CHANNEL)))
    month = _month_name(period)
    expected_stamp = release_day.strftime("%b %d, %Y, 10:00 ET")
    candidates: list[str] = []
    for href, label in index.links:
        normalized = _clean_text(label)
        if (not normalized.startswith(expected_stamp)
                or not re.search(r"\bManufacturing PMI\b", normalized, re.IGNORECASE)
                or not re.search(rf"\b{month}\s+{period[:4]}\b", normalized, re.IGNORECASE)):
            continue
        if not href.startswith("/news-releases/"):
            continue
        full = "https://www.prnewswire.com" + href
        if _allowed_source_url(full):
            candidates.append(full)
    if len(candidates) != 1:
        raise ValueError("OFFICIAL_RELEASE_NOT_FOUND:ISM_PRNEWSWIRE_INDEX")

    url = candidates[0]
    report = _Document()
    report.feed(_decode(session.get(url, exact=url)))
    text = report.text
    title_pattern = re.compile(
        rf"Manufacturing PMI(?:®)?\s+at\s+[\d.]+%;?\s+{month}\s+{period[:4]}\s+"
        rf"ISM(?:®)?\s+Manufacturing PMI(?:®)?\s+Report",
        re.IGNORECASE,
    )
    if not title_pattern.search(text):
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:ISM_PRNEWSWIRE_TITLE")
    if not re.search(r"News provided by\s+Institute for Supply Management\b", text, re.IGNORECASE):
        raise ValueError("OFFICIAL_PROVIDER_MISMATCH:ISM_PRNEWSWIRE")
    published_match = re.search(r'"datePublished"\s*:\s*"([^\"]+)"', text)
    if not published_match:
        raise ValueError("OFFICIAL_PUBLISHED_AT_NOT_FOUND:ISM_PRNEWSWIRE")
    try:
        published = datetime.fromisoformat(published_match.group(1))
    except ValueError as exc:
        raise ValueError("OFFICIAL_PUBLISHED_AT_INVALID:ISM_PRNEWSWIRE") from exc
    if published.tzinfo is None or published.astimezone(_EASTERN) != _release_date_time(release_day, 10):
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:ISM_PRNEWSWIRE_RELEASE_DATE")
    return _ism_value(text, period), url, published.astimezone(ZoneInfo("UTC"))


def _ism_release(session: _FetchSession, period: str, release_day: date) -> tuple[str, str, datetime]:
    target_path = f"/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/{_month_slug(period)}/"
    target_url = "https://www.ismworld.org" + target_path
    # Prefer ISM's own dated report. Some public pages redirect to a member
    # login edge; only a source-fetch failure permits the vendor-channel fallback.
    try:
        raw_report = session.get(target_url, exact=target_url)
    except (OSError, ValueError) as exc:
        if not (isinstance(exc, OSError) or str(exc) == "OFFICIAL_SOURCE_REDIRECT_REJECTED"):
            raise
        return _ism_prnewswire_release(session, period, release_day)
    report = _Document()
    report.feed(_decode(raw_report))
    text = report.text
    expected_title = f"{_month_name(period)} {period[:4]} ISM"
    if expected_title.casefold() not in text.casefold() or "report was issued today" not in text.casefold():
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:ISM_REPORT")
    return _ism_value(text, period), target_url, _release_date_time(release_day, 10).astimezone(ZoneInfo("UTC"))


def _adp_release(session: _FetchSession, period: str, release_day: date) -> tuple[str, str, datetime]:
    index = _Document()
    index.feed(_decode(session.get(ADP_RELEASES_INDEX)))
    month = _month_name(period)
    candidates = []
    for href, label in index.links:
        normalized = _clean_text(label)
        if ("ADP National Employment Report" in normalized
                and "Preliminary Estimate" not in normalized
                and re.search(rf"\b{month}\b", normalized, re.IGNORECASE)):
            full = href if href.startswith("https://") else "https://mediacenter.adp.com" + href
            path_day = re.match(r"/(\d{4}-\d{2}-\d{2})-", urlparse(full).path)
            if _allowed_source_url(full) and path_day and date.fromisoformat(path_day.group(1)) == release_day:
                candidates.append(full)
    if len(candidates) != 1:
        raise ValueError("OFFICIAL_RELEASE_NOT_FOUND:ADP_NER_INDEX")
    url = candidates[0]
    path_day = re.match(r"/(\d{4}-\d{2}-\d{2})-", urlparse(url).path)
    if not path_day or date.fromisoformat(path_day.group(1)) != release_day:
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:ADP_RELEASE_DATE")
    html = _decode(session.get(url))
    page = _Document()
    page.feed(html)
    text = page.text
    if f"{month} {period[:4]} Report Highlights" not in text:
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:ADP_REPORT_MONTH")
    match = re.search(
        r"Change in U\.S\. Private Employment\s*:?\s*([+-]?\s*[\d,]+)\s*(?:jobs)?",
        text, re.IGNORECASE,
    )
    if not match:
        raise ValueError("OFFICIAL_VALUE_NOT_FOUND:ADP_PRIVATE_EMPLOYMENT")
    value = _parse_decimal(match.group(1).replace(" ", ""))
    # ITEMDATE is the provider's own machine-readable release timestamp.
    published_match = re.search(r"ITEMDATE:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+EDT", html)
    if not published_match:
        raise ValueError("OFFICIAL_PUBLISHED_AT_NOT_FOUND:ADP_ITEMDATE")
    published_day = date.fromisoformat(published_match.group(1))
    if published_day != release_day:
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:ADP_RELEASE_DATE")
    hour, minute, second = (int(part) for part in published_match.group(2).split(":"))
    published = datetime.combine(published_day, time(hour, minute, second), _EASTERN).astimezone(ZoneInfo("UTC"))
    return value, url, published


def _conference_board(session: _FetchSession, period: str, release_day: date) -> tuple[str, str, datetime]:
    url = CB_CONFIDENCE_PAGE
    page = _Document()
    html = _decode(session.get(url, exact=url))
    page.feed(html)
    text = page.text
    month, year = _month_name(period), period[:4]
    last_day = date(int(year), int(period[-2:]) % 12 + 1, 1) - timedelta(days=1) if int(period[-2:]) < 12 else date(int(year) + 1, 1, 1) - timedelta(days=1)
    last_tuesday = last_day - timedelta(days=(last_day.weekday() - 1) % 7)
    if release_day != last_tuesday:
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:CONFERENCE_BOARD_SCHEDULE")
    date_match = re.search(r"Updated:\s*(?:Tuesday,\s*)?([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text, re.IGNORECASE)
    if not date_match:
        raise ValueError("OFFICIAL_PUBLISHED_AT_NOT_FOUND:CONFERENCE_BOARD_DATE")
    release_date = date(int(date_match.group(3)), datetime.strptime(date_match.group(1), "%B").month, int(date_match.group(2)))
    if release_date != release_day or f"in {month}".casefold() not in text.casefold():
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:CONFERENCE_BOARD_PAGE")
    match = re.search(
        rf"Consumer Confidence Index[^\n]{{0,180}}?\bto\s*([\d.]+)\s*\(1985=100\)\s*in\s*{month}", text, re.IGNORECASE
    )
    if not match:
        raise ValueError("OFFICIAL_VALUE_NOT_FOUND:CONFERENCE_BOARD_INDEX")
    return _parse_decimal(match.group(1)), url, _release_date_time(release_day, 10).astimezone(ZoneInfo("UTC"))


def _jolts_release(session: _FetchSession, period: str, release_day: date) -> tuple[str, str, datetime]:
    url = f"https://www.bls.gov/news.release/archives/jolts_{release_day:%m%d%Y}.htm"
    page = _Document()
    page.feed(_decode(session.get(url, exact=url)))
    text = page.text
    month, year = _month_name(period), period[:4]
    if f"JOB OPENINGS AND LABOR TURNOVER {month.upper()} {year}".casefold() not in text.casefold():
        # BLS heading commonly uses an en dash and not the full month-year
        # uppercase phrase, so match exact survey heading date independently.
        if not re.search(rf"JOB OPENINGS AND LABOR TURNOVER[^.\n]{{0,30}}{month}\s+{year}", text, re.IGNORECASE):
            raise ValueError("OFFICIAL_PERIOD_MISMATCH:BLS_JOLTS_REFERENCE_MONTH")
    release_match = re.search(
        r"For release\s+(\d{1,2}:\d{2})\s*a\.m\.\s*\(ET\)\s*\w+,\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text, re.IGNORECASE
    )
    if not release_match:
        raise ValueError("OFFICIAL_PUBLISHED_AT_NOT_FOUND:BLS_JOLTS_ARCHIVE")
    published_day = date(int(release_match.group(4)), datetime.strptime(release_match.group(2), "%B").month, int(release_match.group(3)))
    if published_day != release_day:
        raise ValueError("OFFICIAL_PERIOD_MISMATCH:BLS_JOLTS_RELEASE_DATE")
    hour, minute = (int(piece) for piece in release_match.group(1).split(":"))
    published = datetime.combine(published_day, time(hour, minute), _EASTERN).astimezone(ZoneInfo("UTC"))
    expected_header = "Levels (in thousands)"
    matched_value = None
    for table in page.tables:
        is_level_table = any(
            len(row) >= 2 and row[0].casefold() == "industry and region"
            and expected_header.casefold() in row[1].casefold()
            for row in table
        )
        if not is_level_table:
            continue
        target = f"{month[:3]}. {year}".casefold()
        for date_row in table:
            def is_target_month(cell):
                normalized = re.sub(r"\([^)]*\)", "", _clean_text(cell)).casefold().rstrip(".")
                return normalized in {target.rstrip("."), f"{month} {year}".casefold()}

            column = next((i for i, cell in enumerate(date_row) if is_target_month(cell)), None)
            if column is None:
                continue
            # BLS marks the category/stub header with rowspan, so the month
            # header row omits that cell while each data row still starts with
            # its category label. The month index therefore needs one offset.
            if date_row and re.fullmatch(r"[A-Za-z]{3,9}\.\s*\d{4}(?:\s*\([^)]*\))?", _clean_text(date_row[0])):
                column += 1
            for row in table:
                if row and row[0].casefold() == "total" and column < len(row):
                    candidate = row[column]
                    if re.fullmatch(r"[\d,]+", candidate):
                        matched_value = candidate
                        break
            if matched_value:
                break
        if matched_value:
            break
    if matched_value is None:
        raise ValueError("OFFICIAL_VALUE_NOT_FOUND:BLS_JOLTS_TOTAL_JOB_OPENINGS")
    return _parse_decimal(matched_value), url, published


def _period_label(metric: str, period: str) -> str:
    if metric.startswith("bea_final_gdp"):
        return period
    if metric == "dol_unemployment_claims":
        return period
    return period


def _make_result(metric: str, value: str, source_url: str, data_url: str | None,
                 period: str, published: datetime, available: datetime, fetched: datetime) -> dict:
    unit_basis = {
        "bea_final_gdp_qoq": ("percent", "quarter_over_quarter_annualized", "%"),
        "bea_final_gdp_price_index_qoq": ("percent", "quarter_over_quarter", "%"),
        "bea_core_pce_mom": ("percent", "month_over_month", "%"),
        "dol_unemployment_claims": ("persons", "seasonally_adjusted_initial_claims_weekly", ""),
        "ism_manufacturing_pmi": ("diffusion_index", "seasonally_adjusted_manufacturing_survey", ""),
        "adp_nonfarm_employment_change": ("persons", "private_sector_monthly_change", ""),
        "conference_board_consumer_confidence": ("index_1985_100", "seasonally_adjusted_consumer_confidence", ""),
        "bls_jolts_job_openings": ("thousand_job_openings", "seasonally_adjusted_month_end_level", "K"),
    }
    unit, basis, suffix = unit_basis[metric]
    display = f"{value}{suffix}"
    if unit == "percent":
        display = f"{value}%"
    elif unit == "persons":
        display = f"{value} claims" if metric == "dol_unemployment_claims" else f"{value} jobs"
    return {
        "actual": display,
        "actual_value": value,
        "actual_unit": unit,
        "actual_basis": basis,
        "actual_method": "OFFICIAL_REPORTED",
        "actual_is_estimate": False,
        "actual_reference_period": _period_label(metric, period),
        "actual_published_at": iso_utc(published),
        "actual_published_at_source": "official_release_page",
        "actual_available_at": iso_utc(available),
        "actual_fetched_at": iso_utc(fetched),
        "actual_source_url": source_url,
        "actual_data_url": data_url,
        "actual_provider": _PROVIDER[metric],
        "actual_status": "VERIFIED",
        "actual_error": None,
    }


def fetch_release_actual(event: dict, *, fetch=None, now=None, clock=None) -> dict:
    """Fetch and period-bind one supported official release.

    ``fetch(url)`` may be injected by tests. It returns bytes/text, optionally
    paired with a completion timestamp. ``now`` may be a datetime; ``clock``
    provides readback completion time and defaults to UTC wall clock.
    """
    metric = map_release_event(event)
    if metric is None:
        raise ValueError("OFFICIAL_EVENT_UNMAPPED")
    event_at = _event_datetime(event)
    wall_clock = clock or (lambda: datetime.now(ZoneInfo("UTC")))
    now_at = utc_datetime(now if now is not None else wall_clock())
    if event_at > now_at:
        raise ValueError("OFFICIAL_RELEASE_NOT_YET_KNOWN")
    expected = _expected_period(metric, event_at)
    session = _FetchSession(fetch, wall_clock, now_at)

    if metric.startswith("bea_"):
        source_url, body_text = _find_bea_release(session, metric, expected)
        value, _basis = _extract_bea(metric, expected, body_text, session)
        published = _release_time_from_bea(body_text)
        if published.astimezone(_EASTERN).date() != event_at.astimezone(_EASTERN).date():
            raise ValueError("OFFICIAL_PERIOD_MISMATCH:BEA_RELEASE_DATE")
        data_url = BEA_GDP_PRICE_INDEX if metric == "bea_final_gdp_price_index_qoq" else None
    elif metric == "dol_unemployment_claims":
        value, source_url, published = _dol_actual(session, expected, event_at.astimezone(_EASTERN).date())
        data_url = None
    elif metric == "ism_manufacturing_pmi":
        value, source_url, published = _ism_release(session, expected, event_at.astimezone(_EASTERN).date())
        data_url = None
    elif metric == "adp_nonfarm_employment_change":
        value, source_url, published = _adp_release(session, expected, event_at.astimezone(_EASTERN).date())
        data_url = None
    elif metric == "conference_board_consumer_confidence":
        value, source_url, published = _conference_board(session, expected, event_at.astimezone(_EASTERN).date())
        data_url = None
    elif metric == "bls_jolts_job_openings":
        value, source_url, published = _jolts_release(session, expected, event_at.astimezone(_EASTERN).date())
        data_url = None
    else:
        raise ValueError("OFFICIAL_EVENT_UNMAPPED")

    fetched = session.completed_at
    if fetched is None:
        raise ValueError("OFFICIAL_FETCH_COMPLETION_MISSING")
    # available_at denotes completion of the value parse and period checks,
    # while fetched_at denotes the last fully-read source response.
    available = utc_datetime(wall_clock())
    # The calendar's ``now`` is captured before the HTTP work begins. When its
    # live clock was injected, use the post-read clock for future-value checks.
    final_now = utc_datetime(wall_clock())
    if available > final_now:
        raise ValueError("OFFICIAL_FETCH_TIMESTAMP_IN_FUTURE")
    if available < fetched:
        raise ValueError("OFFICIAL_VALIDATION_TIMESTAMP_BEFORE_FETCH")
    if published > available:
        raise ValueError("OFFICIAL_ACTUAL_NOT_AVAILABLE_AT_FETCH")
    if published > final_now:
        raise ValueError("OFFICIAL_PUBLICATION_IN_FUTURE")
    if available < event_at:
        raise ValueError("OFFICIAL_FETCH_BEFORE_EVENT")
    return _make_result(metric, value, source_url, data_url, expected, published, available, fetched)


def official_release_url(event: dict, url: str | None) -> bool:
    """Validate stored release URLs against exact provider paths and period."""
    metric = map_release_event(event)
    if metric is None or not isinstance(url, str) or not _allowed_source_url(url):
        return False
    try:
        day = _event_local_day(event)
        period = _expected_period(metric, _event_datetime(event))
        parsed = urlparse(url)
        path = parsed.path
        if metric.startswith("bea_"):
            if metric == "bea_core_pce_mom":
                month = _month_name(period).lower()
                expected = f"/news/{period[:4]}/personal-income-and-outlays-{month}-{period[:4]}"
                return parsed.hostname in {"www.bea.gov", "bea.gov"} and path == expected
            year, _q, ordinal = _quarter_words(period)
            expected = f"/news/{year}/gdp-third-estimate-industries-corporate-profits-state-gdp-and-state-personal-income-{ordinal}"
            return parsed.hostname in {"www.bea.gov", "bea.gov"} and path == expected
        if metric == "dol_unemployment_claims":
            return parsed.hostname == "www.dol.gov" and path in {"/ui/data.pdf", f"/newsroom/releases/eta/eta{day:%Y%m%d}"}
        if metric == "ism_manufacturing_pmi":
            official_path = f"/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/{_month_slug(period)}/"
            prnewswire_path = re.fullmatch(
                rf"/news-releases/manufacturing-pmi-at-\d+(?:-\d+)?-{_month_slug(period)}-"
                rf"{period[:4]}-ism-manufacturing-pmi-report-\d+\.html",
                path,
            )
            return ((parsed.hostname == "www.ismworld.org" and path == official_path)
                    or (parsed.hostname == "www.prnewswire.com" and prnewswire_path is not None))
        if metric == "adp_nonfarm_employment_change":
            return parsed.hostname == "mediacenter.adp.com" and path.startswith(f"/{day:%Y-%m-%d}-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-")
        if metric == "conference_board_consumer_confidence":
            return parsed.hostname == "www.conference-board.org" and path == "/topics/consumer-confidence/"
        if metric == "bls_jolts_job_openings":
            return parsed.hostname == "www.bls.gov" and path == f"/news.release/archives/jolts_{day:%m%d%Y}.htm"
    except (ValueError, TypeError, OverflowError):
        return False
    return False
