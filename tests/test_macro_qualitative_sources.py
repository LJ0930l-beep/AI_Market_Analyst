"""Speech text cannot masquerade as a numeric release."""
from datetime import UTC, datetime, timedelta

import pytest

from core.macro_qualitative_sources import fetch_official_text, map_qualitative_event

EVENT={'currency':'USD','title':'FOMC Member Waller Speaks','event_time':'2026-10-01T14:00:00+00:00'}
NOW=datetime(2026,10,1,14,1,tzinfo=UTC)

def feed(link='https://www.federalreserve.gov/newsevents/speech/waller20261001a.htm',day='1 Oct 2026'):
    return f'<rss><channel><item><title>Waller, Data and AI</title><link>{link}</link><description>Speech about public economic data</description><pubDate>Thu, {day} 14:00:00 GMT</pubDate></item></channel></rss>'

def test_exact_speaker_day_and_official_host_are_bound():
    calls=[]
    def reader(url):
        calls.append(url)
        return (feed() if url.endswith('.xml') else '<html><h1>Waller, Data and AI</h1></html>',NOW)
    value=fetch_official_text(EVENT,fetch=reader,now=NOW,clock=lambda:NOW)
    assert value['qualitative_status']=='OFFICIAL_TEXT_AVAILABLE'
    assert len(calls)==2
    assert 'actual' not in value
    for body in [feed(day='30 Sep 2026'),feed(link='https://evil.example/waller')]:
        with pytest.raises(ValueError,match='EVENT_NOT_MATCHED'):
            fetch_official_text(EVENT,fetch=lambda _, body=body:body,now=NOW,clock=lambda:NOW)

def test_future_event_never_fetches_and_numeric_event_is_not_speech():
    with pytest.raises(ValueError,match='NOT_YET_RELEASED'):
        fetch_official_text(EVENT,fetch=lambda _:pytest.fail('future fetch'),now=NOW-timedelta(hours=1))
    assert map_qualitative_event({'currency':'USD','title':'Unemployment Rate'}) is None


def test_person_profile_is_not_an_event_and_duplicate_links_are_one_source():
    event = {'currency': 'USD', 'title': 'FOMC Member Kashkari Speaks',
             'event_time': '2026-09-30T14:00:00+00:00'}
    calls = []
    article = 'https://www.minneapolisfed.org/speeches/2026/kashkari-council'
    def reader(url):
        calls.append(url)
        if url.endswith('/topic/monetary-policy'):
            return (('<a href="/people/neel-kashkari">Neel Kashkari</a>'
                    f'<a href="{article}">Kashkari remarks</a>'
                    f'<a href="{article}">Kashkari event</a>'), NOW)
        assert url == article
        return '<h1>Kashkari remarks, September 30, 2026</h1>', NOW
    result = fetch_official_text(event, fetch=reader, now=NOW, clock=lambda:NOW)
    assert result['qualitative_source_url'] == article
    assert result['qualitative_published_at_source'] == 'official_event_date_with_scheduled_time'
    assert len(calls) == 2


def test_delayed_feed_publication_requires_exact_event_date_in_official_text():
    article = 'https://www.federalreserve.gov/newsevents/speech/waller20261001a.htm'
    later = NOW + timedelta(days=1)
    for text, expected in [('<h1>Waller, October 1, 2026</h1>', True),
                           ('<h1>Waller, October 2, 2026</h1>', False)]:
        def reader(url, text=text):
            return (feed(day='2 Oct 2026') if url.endswith('.xml') else text, later)
        if expected:
            result = fetch_official_text(EVENT, fetch=reader, now=later, clock=lambda:later)
            assert result['qualitative_source_url'] == article
        else:
            with pytest.raises(ValueError, match='EVENT_NOT_MATCHED'):
                fetch_official_text(EVENT, fetch=reader, now=later, clock=lambda:later)


def test_newest_event_is_retained_and_recording_is_not_called_a_transcript():
    event = {'currency':'USD','title':'FOMC Member Kashkari Speaks',
             'event_time':'2026-09-30T22:00:00Z'}
    recent = 'https://www.minneapolisfed.org/speeches/2026/kashkari-new'
    def reader(url):
        if url.endswith('/topic/monetary-policy'):
            return ''.join(f'<a href="{href}">Kashkari</a>' for href in [recent,
                'https://www.minneapolisfed.org/speeches/2026/kashkari-old-1',
                'https://www.minneapolisfed.org/speeches/2026/kashkari-old-2',
                'https://www.minneapolisfed.org/speeches/2026/kashkari-old-3']), NOW
        if url == recent:
            return '<h1>Kashkari September 30, 2026</h1><p>Full event video</p>', NOW
        return '<h1>Kashkari September 1, 2026</h1>', NOW
    result = fetch_official_text(event, fetch=reader, now=NOW, clock=lambda:NOW)
    assert result['qualitative_source_url'] == recent
    assert result['qualitative_status'] == 'OFFICIAL_RECORDING_AVAILABLE'
    assert 'actual' not in result
