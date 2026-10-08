"""Public, period-bound macro actual sources.

Forex Factory's public calendar export is treated as a schedule only.  This
module reads a small, explicit set of official statistical releases and never
turns a release into a directional trading instruction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
import json
import re
from urllib.request import Request, urlopen

BLS_API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_SERIES = {
    "unemployment": "LNS14000000",
    "nonfarm_level": "CES0000000001",
    "hourly_earnings": "CES0500000003",
}
EUROSTAT_HOST = "ec.europa.eu"
MAX_SOURCE_BYTES = 1_000_000


def utc_datetime(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def month_before(value: datetime) -> str:
    year, month = value.year, value.month - 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def eurostat_flash_reference_period(value: datetime) -> str:
    """Eurostat flash releases are late-month estimates for that same month."""
    at = utc_datetime(value)
    return f"{at.year:04d}-{at.month:02d}" if at.day >= 20 else month_before(at)


def shift_month(period: str, delta: int) -> str:
    year, month = (int(part) for part in period.split("-", 1))
    absolute = year * 12 + month - 1 + delta
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def map_macro_event(event: dict) -> str | None:
    """Return a supported metric key, or None for an intentionally unmapped row."""
    title = re.sub(r"\s+", " ", str(event.get("title", ""))).strip().lower()
    currency = re.sub(r"[^a-z]", "", str(event.get("currency", "")).lower())
    if currency in {"usd", "us", "unitedstates", "unitedstatesofamerica"}:
        if re.fullmatch(r"non[- ]farm employment change", title):
            return "bls_nonfarm_change"
        if title == "unemployment rate":
            return "bls_unemployment_rate"
        if title == "average hourly earnings m/m":
            return "bls_hourly_earnings_mom"
        if title == "average hourly earnings y/y":
            return "bls_hourly_earnings_yoy"
    if currency in {"eur", "eu", "eurozone", "euroarea"}:
        if title == "cpi flash estimate m/m":
            return "eurostat_hicp_mom"
        if title == "cpi flash estimate y/y":
            return "eurostat_hicp_yoy"
        if title == "core cpi flash estimate y/y":
            return "eurostat_core_hicp_yoy"
    return None


def required_bls_periods(metric: str, reference_period: str) -> tuple[str, ...]:
    if metric == "bls_unemployment_rate":
        return (reference_period,)
    if metric in {"bls_nonfarm_change", "bls_hourly_earnings_mom"}:
        return (reference_period, shift_month(reference_period, -1))
    if metric == "bls_hourly_earnings_yoy":
        return (reference_period, shift_month(reference_period, -12))
    return ()


def parse_bls_response(payload: dict) -> dict[str, dict[str, str]]:
    """Normalize a BLS v1 API response to {series_id: {YYYY-MM: value}}."""
    if not isinstance(payload, dict) or payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS_REQUEST_NOT_SUCCEEDED")
    results = payload.get("Results") or payload.get("results")
    series = results.get("series") if isinstance(results, dict) else None
    if not isinstance(series, list):
        raise ValueError("BLS_SERIES_RESPONSE_INVALID")
    output: dict[str, dict[str, str]] = {}
    for item in series:
        if not isinstance(item, dict):
            continue
        series_id = str(item.get("seriesID", item.get("series_id", "")))
        rows = item.get("data")
        if series_id not in BLS_SERIES.values() or not isinstance(rows, list):
            continue
        output[series_id] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            period = str(row.get("period", ""))
            year = str(row.get("year", ""))
            if not re.fullmatch(r"M(?:0[1-9]|1[0-2])", period) or not re.fullmatch(r"\d{4}", year):
                continue
            try:
                value = Decimal(str(row.get("value")))
            except (InvalidOperation, TypeError):
                continue
            if not value.is_finite():
                continue
            output[series_id][f"{year}-{int(period[1:]):02d}"] = decimal_text(value)
    return output


def decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    rendered = format(normalized, "f")
    return "0" if rendered in {"-0", ""} else rendered


def _percent_change(current: Decimal, prior: Decimal) -> Decimal:
    if not prior.is_finite() or prior == 0:
        raise ValueError("BLS_PRIOR_VALUE_INVALID")
    return ((current / prior) - Decimal("1")) * Decimal("100")


def format_percent(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return format(rounded, ".1f")


def actual_from_bls(metric: str, reference_period: str, data: dict[str, dict[str, str]]) -> dict:
    if metric == "bls_unemployment_rate":
        value = data.get(BLS_SERIES["unemployment"], {}).get(reference_period)
        if value is None:
            raise KeyError("BLS_EXPECTED_PERIOD_MISSING")
        return {
            "actual": f"{value}%", "actual_value": value,
            "actual_unit": "percent_of_labor_force",
            "actual_basis": "seasonally_adjusted", "actual_method": "OFFICIAL_REPORTED",
        }
    if metric == "bls_nonfarm_change":
        current = data.get(BLS_SERIES["nonfarm_level"], {}).get(reference_period)
        previous = data.get(BLS_SERIES["nonfarm_level"], {}).get(shift_month(reference_period, -1))
        if current is None or previous is None:
            raise KeyError("BLS_EXPECTED_PERIOD_MISSING")
        change = Decimal(current) - Decimal(previous)
        return {
            "actual": f"{decimal_text(change)}K", "actual_value": decimal_text(change),
            "actual_unit": "thousand_persons",
            "actual_basis": "seasonally_adjusted_monthly_change",
            "actual_method": "DERIVED_FROM_OFFICIAL_SERIES",
        }
    if metric in {"bls_hourly_earnings_mom", "bls_hourly_earnings_yoy"}:
        current = data.get(BLS_SERIES["hourly_earnings"], {}).get(reference_period)
        prior_period = shift_month(reference_period, -1 if metric.endswith("mom") else -12)
        prior = data.get(BLS_SERIES["hourly_earnings"], {}).get(prior_period)
        if current is None or prior is None:
            raise KeyError("BLS_EXPECTED_PERIOD_MISSING")
        actual = format_percent(_percent_change(Decimal(current), Decimal(prior)))
        return {
            "actual": f"{actual}%", "actual_value": actual, "actual_unit": "percent",
            "actual_basis": "month_over_month" if metric.endswith("mom") else "year_over_year",
            "actual_method": "DERIVED_FROM_OFFICIAL_SERIES",
        }
    raise ValueError("BLS_METRIC_UNMAPPED")


def bls_release_url(event_time: datetime) -> str:
    release_date = event_time.astimezone(timezone.utc)
    return f"https://www.bls.gov/news.release/archives/empsit_{release_date:%m%d%Y}.htm"


def fetch_bls_series(start_year: int, end_year: int) -> tuple[dict, datetime]:
    """One credential-free v1 POST for all needed series and years."""
    if not (1900 <= start_year <= end_year <= datetime.now(timezone.utc).year + 1):
        raise ValueError("BLS_YEAR_RANGE_INVALID")
    payload = {
        "seriesid": list(BLS_SERIES.values()),
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    request = Request(
        BLS_API_URL,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AI-Market-Analyst/2.0 (public macro actuals)"},
        method="POST",
    )
    with urlopen(request, timeout=12) as response:
        if response.geturl() != BLS_API_URL:
            raise ValueError("BLS_SOURCE_REDIRECT_REJECTED")
        raw = response.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("BLS_RESPONSE_TOO_LARGE")
    payload = json.loads(raw)
    parse_bls_response(payload)
    # Timestamp after the response has been fully read, decoded, and validated.
    completed_at = datetime.now(timezone.utc)
    return payload, completed_at


def eurostat_flash_url(event_time: datetime) -> str:
    day = event_time.astimezone(timezone.utc)
    return f"https://ec.europa.eu/eurostat/en/web/products-euro-indicators/w/2-{day:%d%m%Y}-ap"


def fetch_eurostat_page(url: str) -> tuple[str, datetime]:
    if not url.startswith("https://ec.europa.eu/eurostat/"):
        raise ValueError("EUROSTAT_SOURCE_URL_REJECTED")
    request = Request(url, headers={"User-Agent": "AI-Market-Analyst/2.0 (public macro actuals)"})
    with urlopen(request, timeout=12) as response:
        if response.geturl().split("/", 3)[:3] != ["https:", "", EUROSTAT_HOST]:
            raise ValueError("EUROSTAT_SOURCE_REDIRECT_REJECTED")
        raw = response.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("EUROSTAT_RESPONSE_TOO_LARGE")
    return raw.decode("utf-8", errors="replace"), datetime.now(timezone.utc)


class _EurostatPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self.jsonld: list[str] = []
        self.tables: list[list[list[str]]] = []
        self.table_colspans: list[list[list[int]]] = []
        self._in_title = False
        self._in_script = False
        self._script_parts: list[str] = []
        self._table_depth = 0
        self._row: list[str] | None = None
        self._row_colspans: list[int] | None = None
        self._cell: list[str] | None = None
        self._cell_colspan = 1
        self._table: list[list[str]] | None = None
        self._table_colspans: list[list[int]] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "title":
            self._in_title = True
        elif tag.lower() == "script" and "ld+json" in str(attrs.get("type", "")).lower():
            self._in_script = True
            self._script_parts = []
        elif tag.lower() == "table":
            if self._table_depth == 0:
                self._table = []
                self._table_colspans = []
            self._table_depth += 1
        elif tag.lower() == "tr" and self._table_depth and self._row is None:
            self._row = []
            self._row_colspans = []
        elif tag.lower() in {"th", "td"} and self._row is not None and self._cell is None:
            self._cell = []
            try:
                self._cell_colspan = max(1, min(64, int(attrs.get("colspan", "1"))))
            except (TypeError, ValueError):
                self._cell_colspan = 1

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False
        elif tag.lower() == "script" and self._in_script:
            self._in_script = False
            self.jsonld.append("".join(self._script_parts))
        elif tag.lower() in {"th", "td"} and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            if self._row_colspans is not None:
                self._row_colspans.append(self._cell_colspan)
            self._cell = None
            self._cell_colspan = 1
        elif tag.lower() == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            if self._table_colspans is not None and self._row_colspans is not None:
                self._table_colspans.append(self._row_colspans)
            self._row = None
            self._row_colspans = None
        elif tag.lower() == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._table is not None:
                self.tables.append(self._table)
                self.table_colspans.append(self._table_colspans or [])
                self._table = None
                self._table_colspans = None

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._in_script:
            self._script_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if data.strip():
            self.body_parts.append(data.strip())


def _jsonld_values(parser: _EurostatPageParser) -> list[str]:
    values = []
    for script in parser.jsonld:
        try:
            decoded = json.loads(script)
        except (TypeError, ValueError):
            continue
        objects = decoded if isinstance(decoded, list) else [decoded]
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            for key in ("headline", "name", "description", "datePublished", "dateCreated"):
                if obj.get(key) is not None:
                    values.append(str(obj[key]))
            publisher = obj.get("publisher")
            if isinstance(publisher, dict):
                values.extend(str(publisher.get(key, "")) for key in ("name", "url"))
    return values


def _parse_percent_cell(value: str) -> tuple[str, bool]:
    text = re.sub(r"\s+", " ", str(value)).strip()
    match = re.search(r"[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)", text)
    if not match:
        raise ValueError("EUROSTAT_VALUE_INVALID")
    number = match.group(0).replace(",", ".")
    estimate = bool(re.search(r"(?:\be\b|e\s*$|estimated)", text, flags=re.IGNORECASE))
    return decimal_text(Decimal(number)), estimate


def _expand_spans(row, spans):
    expanded = []
    for index, cell in enumerate(row):
        span = spans[index] if index < len(spans) else 1
        expanded.extend([cell] * max(1, span))
    return expanded


def _period_header(value: str):
    cleaned = re.sub(r"[.,]", " ", str(value)).strip()
    match = re.fullmatch(r"([A-Za-z]{3,9})\s*(\d{2}|\d{4})", cleaned)
    if not match:
        return None
    try:
        month = datetime.strptime(match.group(1)[:3].title(), "%b").month
        year = int(match.group(2))
        if year < 100:
            year += 2000 if year < 70 else 1900
        return f"{year:04d}-{month:02d}"
    except ValueError:
        return None


def parse_eurostat_flash(html: str, event: dict, *, fetched_at: datetime, now: datetime) -> dict:
    """Parse the exact dated Eurostat flash release, not a current revised dataset."""
    parser = _EurostatPageParser()
    parser.feed(html)
    metadata = _jsonld_values(parser)
    title = " ".join(parser.title_parts)
    all_text = " ".join([title, *metadata, *parser.body_parts])
    event_at = utc_datetime(event["event_time"])
    expected_period = eurostat_flash_reference_period(event_at)
    year, month = (int(part) for part in expected_period.split("-"))
    month_name = datetime(year, month, 1).strftime("%B")
    if "flash estimate" not in all_text.lower():
        raise ValueError("EUROSTAT_NOT_FLASH_RELEASE")
    if not re.search(rf"\b{re.escape(month_name)}\s+{year}\b", all_text, flags=re.IGNORECASE):
        raise ValueError("EUROSTAT_REFERENCE_PERIOD_MISMATCH")
    published_match = re.search(
        r"\b(?:datePublished|dateCreated)\b[^\n]{0,80}?((?:19|20)\d{2}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))",
        " ".join(metadata), flags=re.IGNORECASE,
    )
    published_at = None
    if published_match:
        published_at = utc_datetime(published_match.group(1))
    else:
        # JSON-LD values were flattened above; scan raw JSON-LD for exact publish time.
        for raw in parser.jsonld:
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw)
            if match:
                try:
                    published_at = utc_datetime(match.group(1))
                    break
                except ValueError:
                    pass
    if published_at is None:
        raise ValueError("EUROSTAT_PUBLICATION_TIME_MISSING")
    if published_at > utc_datetime(now):
        raise ValueError("EUROSTAT_RELEASE_NOT_YET_AVAILABLE")
    if published_at.date() != event_at.date():
        raise ValueError("EUROSTAT_RELEASE_EVENT_MISMATCH")
    if utc_datetime(fetched_at) < published_at:
        raise ValueError("EUROSTAT_FETCH_PRECEDES_RELEASE")

    metric = map_macro_event(event)
    if metric not in {"eurostat_hicp_mom", "eurostat_hicp_yoy", "eurostat_core_hicp_yoy"}:
        raise ValueError("EUROSTAT_METRIC_UNMAPPED")
    values = []
    for table_index, table in enumerate(parser.tables):
        span_rows = parser.table_colspans[table_index] if table_index < len(parser.table_colspans) else []
        target_label = (r"(?:all[- ]items\s+excluding:?\s*)?energy,\s*food,\s*alcohol\s*(?:&|and)\s*tobacco"
                        if metric == "eurostat_core_hicp_yoy" else r"all[- ]items\s+hicp")
        data_rows = [
            (row_index, row) for row_index, row in enumerate(table)
            if row and re.fullmatch(target_label, row[0].strip(), flags=re.IGNORECASE)
        ]
        for data_index, data_row in data_rows:
            for header_index in range(data_index):
                raw_spans = span_rows[header_index] if header_index < len(span_rows) else []
                group_row = _expand_spans(table[header_index], raw_spans)
                lowered = [cell.lower() for cell in group_row]
                annual_columns = [i for i, cell in enumerate(lowered) if "annual rate" in cell]
                monthly_columns = [i for i, cell in enumerate(lowered) if "monthly rate" in cell]
                if not annual_columns or not monthly_columns:
                    continue
                for period_index in range(header_index + 1, data_index):
                    period_spans = span_rows[period_index] if period_index < len(span_rows) else []
                    period_row = _expand_spans(table[period_index], period_spans)
                    for column, label in enumerate(period_row):
                        if _period_header(label) != expected_period:
                            continue
                        if column >= len(data_row) or column >= len(group_row):
                            continue
                        group = group_row[column].lower()
                        if "annual rate" in group:
                            values.append(("annual", data_row[column]))
                        elif "monthly rate" in group:
                            values.append(("monthly", data_row[column]))
    annual_values = [value for kind, value in values if kind == "annual"]
    monthly_values = [value for kind, value in values if kind == "monthly"]
    if len(annual_values) != 1 or len(monthly_values) != 1:
        raise ValueError("EUROSTAT_ALL_ITEMS_ROW_AMBIGUOUS")
    annual_text, monthly_text = annual_values[0], monthly_values[0]
    annual_value, annual_estimate = _parse_percent_cell(annual_text)
    monthly_value, monthly_estimate = _parse_percent_cell(monthly_text)
    is_yoy = metric.endswith("yoy")
    return {
        "actual": f"{annual_value if is_yoy else monthly_value}%",
        "actual_value": annual_value if is_yoy else monthly_value,
        "actual_unit": "percent",
        "actual_basis": "year_over_year" if is_yoy else "month_over_month",
        "actual_method": "OFFICIAL_REPORTED",
        "actual_is_estimate": annual_estimate if is_yoy else monthly_estimate,
        "actual_reference_period": expected_period,
        "actual_published_at": iso_utc(published_at),
        "actual_available_at": iso_utc(utc_datetime(fetched_at)),
    }
