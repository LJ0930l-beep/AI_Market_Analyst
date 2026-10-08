"""Official speech/statement discovery, separate from numeric actuals."""
import re
import ssl
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .macro_actuals import iso_utc, utc_datetime

SOURCES = {
    ("USD", "FOMC Member Waller Speaks"): ("Federal Reserve", "https://www.federalreserve.gov/feeds/speeches.xml", "waller"),
    ("USD", "FOMC Member Kashkari Speaks"): ("Minneapolis Fed", "https://www.minneapolisfed.org/topic/monetary-policy", "kashkari"),
    ("EUR", "ECB President Lagarde Speaks"): ("ECB", "https://www.ecb.europa.eu/rss/press.html", "lagarde"),
    ("GBP", "BOE Gov Bailey Speaks"): ("Bank of England", "https://www.bankofengland.co.uk/rss/speeches", "bailey"),
    ("CHF", "SNB Chairman Schlegel Speaks"): ("SNB", "https://www.snb.ch/public/rss/en/speeches", "schlegel"),
    ("USD", "President Trump Speaks"): ("White House", "https://www.whitehouse.gov/remarks/", "trump"),
    ("AUD", "RBA Rate Statement"): ("RBA", "https://www.rba.gov.au/monetary-policy/int-rate-decisions/", "statement"),
    ("AUD", "RBA Press Conference"): ("RBA", "https://www.rba.gov.au/monetary-policy/media-conferences/", "conference"),
}


def map_qualitative_event(event):
    return SOURCES.get((str(event.get("currency", "")).upper(), str(event.get("title", "")).strip()))


def official_text_url(event, url):
    source = map_qualitative_event(event)
    parsed = urlparse(str(url))
    return bool(source and parsed.scheme == "https" and parsed.hostname == urlparse(source[1]).hostname
                and not parsed.username and not parsed.password and parsed.port in (None, 443))


def _article_url(event, url):
    if not official_text_url(event, url):
        return False
    path = re.sub(r"/+", "/", urlparse(url).path)
    prefixes = {"Federal Reserve": "/newsevents/speech/", "Minneapolis Fed": "/speeches/",
                "ECB": "/press/key/date/", "Bank of England": "/speech/",
                "SNB": "/en/publications/communication/speeches/", "White House": "/remarks/"}
    provider = map_qualitative_event(event)[0]
    return path.startswith(prefixes.get(provider, "/not-a-speech/")) and len(path.rstrip("/")) > len(prefixes.get(provider, ""))


def _read(url):
    context = ssl.create_default_context()
    # Augment OS roots, never disable TLS verification.
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except ImportError:
        pass
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Market-Analyst/2.0)",
                                    "Accept": "application/rss+xml,application/xml,text/html;q=0.9,*/*;q=0.8"})
    class OfficialRedirects(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urlparse(newurl)
            if (target.scheme != "https" or target.hostname != urlparse(url).hostname
                    or target.username or target.password or target.port not in (None, 443)):
                raise ValueError("OFFICIAL_TEXT_REDIRECT_REJECTED")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener = build_opener(HTTPSHandler(context=context), OfficialRedirects())
    with opener.open(request, timeout=12) as response:
        if urlparse(response.geturl()).hostname != urlparse(url).hostname:
            raise ValueError("OFFICIAL_TEXT_REDIRECT_REJECTED")
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("OFFICIAL_TEXT_TOO_LARGE")
    return raw.decode("utf-8-sig", "replace"), datetime.now(UTC)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.text, self.meta = [], [], {}
        self.current = None
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "a" and attr.get("href"):
            self.current = [attr["href"], []]
        if tag == "meta":
            self.meta[attr.get("property", attr.get("name", ""))] = attr.get("content", "")
    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag == "a" and self.current:
            self.links.append((self.current[0], " ".join(self.current[1])))
            self.current = None
    def handle_data(self, data):
        if not self.skip and data.strip():
            self.text.append(data.strip())
            if self.current:
                self.current[1].append(data.strip())


def fetch_official_text(event, *, fetch=None, now=None, clock=None):
    source = map_qualitative_event(event)
    if source is None:
        raise ValueError("OFFICIAL_TEXT_UNMAPPED")
    provider, index, speaker = source
    point = utc_datetime(now or datetime.now(UTC))
    event_at = utc_datetime(event["event_time"])
    if event_at > point:
        raise ValueError("OFFICIAL_TEXT_NOT_YET_RELEASED")
    reader = fetch or _read
    def read(url):
        if not official_text_url(event, url):
            raise ValueError("OFFICIAL_TEXT_URL_REJECTED")
        value = reader(url)
        body, available = value if isinstance(value, tuple) else (value, (clock or (lambda: point))())
        if not isinstance(body, str) or len(body.encode("utf-8")) > 1_000_000:
            raise ValueError("OFFICIAL_TEXT_BODY_INVALID")
        return body, utc_datetime(available)
    body, _fetched = read(index)
    candidates = []
    try:
        root = ET.fromstring(body)
        for item in list(root.iter())[:5000]:
            if item.tag.split("}")[-1] != "item":
                continue
            fields = {child.tag.split("}")[-1]: "".join(child.itertext()) for child in item}
            title, description = fields.get("title", ""), fields.get("description", "")
            date_text = fields.get("pubDate", fields.get("date", ""))
            try:
                published = parsedate_to_datetime(date_text) if "," in date_text else utc_datetime(date_text)
                published = utc_datetime(published)
            except (ValueError, TypeError):
                continue
            if (speaker in (title + " " + description).lower()
                    and abs((published.date() - event_at.date()).days) <= 1):
                candidates.append((fields.get("link", ""), title, published, description))
    except ET.ParseError:
        page = _Page(); page.feed(body)
        for href, title in page.links:
            url = urljoin(index, href)
            if speaker in (title + " " + url).lower() and _article_url(event, url) and url != index:
                candidates.append((url, title, None, ""))
    # Index pages often link the same speech from title, image and category.
    # Repeated links are one source, not ambiguous independent publications.
    candidates = list({item[0]: item for item in candidates}.values())[:3]
    matched = []
    for url, title, published, description in candidates[:3]:
        if not _article_url(event, url):
            continue
        article, observed = read(url)
        page = _Page(); page.feed(article)
        time_source = "official_feed"
        if published is None:
            date_text = page.meta.get("article:published_time", page.meta.get("date", ""))
            try:
                published = utc_datetime(date_text)
                time_source = "official_page_metadata"
            except (ValueError, TypeError):
                text = " ".join(page.text)
                if not re.search(rf"\b{event_at:%B}\s+{event_at.day},?\s+{event_at.year}\b", text):
                    continue
                published = event_at
                time_source = "official_event_date_with_scheduled_time"
        completed = utc_datetime((clock or (lambda: datetime.now(UTC)))())
        if published.date() != event_at.date():
            text = " ".join(page.text)
            date_pattern = rf"\b(?:{event_at:%B}\s+{event_at.day},?\s+{event_at.year}|{event_at.day}\s+{event_at:%B}\s+{event_at.year})\b"
            if not re.search(date_pattern, text, re.IGNORECASE):
                continue  # Delayed publication must explicitly date the event.
        if not published <= observed <= completed:
            continue
        if speaker not in (title + " " + " ".join(page.text)).lower():
            continue
        text = " ".join(page.text)
        recording_only = (provider == "Minneapolis Fed"
                          and bool(re.search(r"(?:full\s+event\s*(?:\(|\[)?\s*video|video of (?:the )?full event)", text, re.IGNORECASE))
                          and "transcript" not in text.lower())
        summary = re.sub(r"\s+", " ", description).strip()[:600]
        matched.append({"qualitative_status": "OFFICIAL_RECORDING_AVAILABLE" if recording_only else "OFFICIAL_TEXT_AVAILABLE", "qualitative_provider": provider,
                        "qualitative_source_url": url, "qualitative_title": title[:240],
                        "qualitative_summary": summary,
                        "qualitative_published_at": iso_utc(published),
                        "qualitative_published_at_source": time_source,
                        "qualitative_available_at": iso_utc(observed)})
    if len(matched) != 1:
        raise ValueError("OFFICIAL_TEXT_EVENT_NOT_MATCHED" if not matched else "OFFICIAL_TEXT_EVENT_AMBIGUOUS")
    return matched[0]
