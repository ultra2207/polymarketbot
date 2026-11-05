"""Pure-parser tests for the news stream (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from polymarketbot.data.streams.news import (
    Deduper,
    event_id_from_market,
    parse_gamma_comment,
    parse_iso_ms,
    parse_rfc822_ms,
    parse_rss_feed,
    source_host,
)


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------- RSS
RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>CoinDesk</title>
    <link>https://www.coindesk.com/</link>
    <language>en</language>
    <item>
      <title>Bitcoin rallies into the midterms</title>
      <link>https://www.coindesk.com/markets/2026/11/03/bitcoin-rallies</link>
      <description>The market moved.</description>
      <pubDate>Tue, 03 Nov 2026 21:14:45 GMT</pubDate>
      <guid isPermaLink="false">cd-1</guid>
    </item>
    <item>
      <title>No date here</title>
      <link>https://www.coindesk.com/markets/2026/11/03/no-date</link>
      <guid>cd-2</guid>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">
  <title>Example Atom</title>
  <updated>2026-11-03T22:00:00Z</updated>
  <entry>
    <title>Atom entry one</title>
    <link href="https://example.org/posts/one"/>
    <link rel="self" href="https://example.org/posts/one.atom"/>
    <id>tag:example.org,2026:1</id>
    <updated>2026-11-03T21:14:45Z</updated>
    <summary>Summary one</summary>
  </entry>
  <entry>
    <title>Atom entry without link</title>
    <id>tag:example.org,2026:2</id>
    <published>2026-11-03T22:05:00Z</published>
    <content>Content body</content>
  </entry>
</feed>
"""


def test_parse_rss_20_fixture() -> None:
    rows = parse_rss_feed(
        RSS_FIXTURE, "https://www.coindesk.com/arc/outboundfeeds/rss/", 7, 8
    )

    assert len(rows) == 2
    first = rows[0]
    assert first["source"] == "coindesk.com"
    assert first["title"] == "Bitcoin rallies into the midterms"
    assert first["url"] == "https://www.coindesk.com/markets/2026/11/03/bitcoin-rallies"
    assert first["published_ts_ms"] == _ms(2026, 11, 3, 21, 14, 45)
    assert first["summary"] == "The market moved."
    assert first["language"] == "en"
    assert first["recv_ts_ns"] == 7

    second = rows[1]
    assert second["title"] == "No date here"
    assert second["published_ts_ms"] is None
    assert second["summary"] is None
    assert second["url"].endswith("/no-date")


def test_parse_atom_fixture() -> None:
    rows = parse_rss_feed(ATOM_FIXTURE, "https://example.org/feed.xml", 1, 2)

    assert len(rows) == 2
    first = rows[0]
    assert first["source"] == "example.org"
    assert first["title"] == "Atom entry one"
    assert first["url"] == "https://example.org/posts/one"
    assert first["published_ts_ms"] == _ms(2026, 11, 3, 21, 14, 45)
    assert first["summary"] == "Summary one"
    assert first["language"] == "en"

    second = rows[1]
    assert second["url"] == "tag:example.org,2026:2"  # id is the fallback anchor
    assert second["published_ts_ms"] == _ms(2026, 11, 3, 22, 5)
    assert second["summary"] == "Content body"


def test_parse_rss_accepts_bytes_and_rejects_non_xml() -> None:
    rows = parse_rss_feed(RSS_FIXTURE.encode("utf-8"), "https://www.google.com/", 1, 2)
    assert len(rows) == 2
    assert source_host("https://www.google.com/") == "google.com"
    with pytest.raises(Exception):
        parse_rss_feed("<rss><channel><item>", "https://x.test/", 1, 2)
    # Well-formed HTML is not a feed: no items, no invented rows.
    assert parse_rss_feed("<html><body>not a feed</body></html>", "https://x.test/", 1, 2) == []


def test_timestamp_parsers() -> None:
    assert parse_rfc822_ms("Tue, 03 Nov 2026 21:14:45 GMT") == _ms(2026, 11, 3, 21, 14, 45)
    assert parse_rfc822_ms("garbage") is None
    assert parse_iso_ms("2026-11-03T21:14:45.555428Z") == _ms(2026, 11, 3, 21, 14, 45) + 555
    assert parse_iso_ms("2026-11-03T21:14:45+00:00") == _ms(2026, 11, 3, 21, 14, 45)
    assert parse_iso_ms("") is None


# ------------------------------------------------------------------------ dedupe
def test_deduper() -> None:
    dedupe = Deduper(limit=2)
    assert dedupe.add("https://a.test/1") is True
    assert dedupe.add("https://a.test/1") is False
    assert dedupe.add("") is False
    assert len(dedupe) == 1

    assert dedupe.add("https://a.test/2") is True
    assert dedupe.add("https://a.test/3") is True  # evicts the first key
    assert "https://a.test/1" not in dedupe
    assert "https://a.test/3" in dedupe
    assert len(dedupe) == 2


# --------------------------------------------------------------- Gamma comments
def test_parse_gamma_comment() -> None:
    raw = {
        "id": "2075368",
        "body": "I don't understand why I lost money here.",
        "parentEntityType": "Event",
        "parentEntityID": 18396,
        "userAddress": "0xabc",
        "createdAt": "2025-11-18T20:29:01.555428Z",
        "profile": {"name": "wazat911", "pseudonym": "Charming-Trailer"},
        "reactions": [],
    }
    row = parse_gamma_comment(raw, 3, 4)

    assert row is not None
    assert row["comment_id"] == "2075368"
    assert row["parent_entity_type"] == "Event"
    assert row["parent_entity_id"] == "18396"
    assert row["body"].startswith("I don't understand")
    assert row["created_at_ms"] == _ms(2025, 11, 18, 20, 29, 1) + 555
    assert row["author"] == "wazat911"
    assert json.loads(row["raw_json"])["id"] == "2075368"
    assert parse_gamma_comment({}, 1, 2) is None


def test_parse_gamma_comment_fallbacks() -> None:
    raw = {
        "id": 5,
        "body": "anonymous",
        "parentEntityID": 42,
        "createdAt": "2025-11-18T20:29:01Z",
        "profile": {"pseudonym": "Anon-Trailer"},
        "userAddress": "0xdef",
    }
    row = parse_gamma_comment(raw, 1, 2, "Event", "42")
    assert row["author"] == "Anon-Trailer"
    assert row["parent_entity_type"] == "Event"
    assert row["parent_entity_id"] == "42"


def test_event_id_from_market() -> None:
    payload = [
        {
            "conditionId": "0xabc",
            "events": [{"id": "32228", "ticker": "balance-of-power-2026-midterms"}],
        }
    ]
    assert event_id_from_market(payload) == "32228"
    assert event_id_from_market({"events": [{"id": 7}]}) == "7"
    assert event_id_from_market({"eventId": "99"}) == "99"
    assert event_id_from_market([{"events": []}]) is None
    assert event_id_from_market([]) is None
    assert event_id_from_market(None) is None
