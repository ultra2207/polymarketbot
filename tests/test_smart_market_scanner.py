from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from polymarketbot.data.universe import (
    SENTINEL_CRYPTO_ASSETS,
    SENTINEL_EXPIRY_GRACE_SECONDS,
    UniverseState,
    UniverseUpdate,
)


def _sample_market_row(
    cid: str,
    slug: str = "btc-updown-5m-1793968800",
    question: str = "Bitcoin Up or Down - 5m",
    end_date: str | None = None,
    daily: float = 0.0,
    volume: str = "100.0",
) -> dict:
    if end_date is None:
        end_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    return {
        "conditionId": cid,
        "closed": False,
        "archived": False,
        "clobTokenIds": json.dumps([f"{cid}-yes", f"{cid}-no"]),
        "question": question,
        "slug": slug,
        "endDate": end_date,
        "volume": volume,
        "liquidity": "50",
        "negRisk": False,
        "rewardsMinSize": "1",
        "rewardsMaxSpread": "3.5",
        "clobRewards": [],
    }


def test_matches_sentinel_with_updown_slugs(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", sentinel_enabled=True)

    # 1. Matches standard question pattern (BTC/ETH/SOL)
    btc_row = {"question": "Bitcoin Above $100k?", "slug": "btc-above-100k"}
    assert state._matches_sentinel(btc_row) is True

    # 2. Matches new updown tickers (DOGE, XRP, BNB)
    doge_row = {"question": "Dogecoin Up or Down", "slug": "doge-updown-5m-1793968800"}
    assert state._matches_sentinel(doge_row) is True

    xrp_row = {"question": "XRP Up or Down", "slug": "xrp-updown-15m-1793968200"}
    assert state._matches_sentinel(xrp_row) is True

    # 3. Non-crypto updown does not match
    politics_row = {"question": "Will Trump sign bill?", "slug": "trump-signs-bill"}
    assert state._matches_sentinel(politics_row) is False


def test_deterministic_sentinel_generation(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", sentinel_enabled=True)

    mock_events = [
        {
            "id": 1234,
            "slug": "btc-updown-5m-1793968800",
            "title": "Bitcoin Up or Down - 5m",
            "markets": [
                {
                    "conditionId": "0xbtc5m",
                    "question": "Bitcoin Up or Down - 5m",
                    "slug": "btc-updown-5m-1793968800",
                    "endDate": "2026-11-06T12:45:00Z",
                    "clobTokenIds": ["tok1", "tok2"],
                }
            ],
        }
    ]

    with patch.object(state, "_get_json", return_value={"events": mock_events}) as mock_get:
        mkts = state._fetch_deterministic_sentinels()
        assert len(mkts) == 1
        assert mkts[0]["conditionId"] == "0xbtc5m"
        assert mkts[0]["event_slug"] == "btc-updown-5m-1793968800"

        # Verify query parameters included target crypto assets
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        params = kwargs.get("params") or args[1]
        slug_values = [v for k, v in params if k == "slug"]
        assert any(s.startswith("btc-updown-5m-") for s in slug_values)
        assert any(s.startswith("eth-updown-5m-") for s in slug_values)
        assert any(s.startswith("sol-updown-5m-") for s in slug_values)


def test_poll_incremental_discovers_short_duration_markets(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", poll_seconds=15, sentinel_enabled=True)
    state.last_market_id = 9999999  # Large last_market_id to simulate previous crawl

    # Newly active 5-minute market has a lower id or independent creation
    mkt_5m = _sample_market_row("0x5m_active", slug="eth-updown-5m-1793968800", question="ETH Up or Down - 5m")

    with patch.object(state, "_fetch_short_duration_markets", return_value=[mkt_5m]), \
         patch.object(state, "_fetch_deterministic_sentinels", return_value=[]), \
         patch.object(state, "_fetch_market_pages", return_value=[]):

        update = asyncio.run(state.poll_incremental())

        assert update is not None
        assert "0x5m_active" in state.markets
        m = state.markets["0x5m_active"]
        assert m.sentinel is True
        assert m.tokens == ("0x5m_active-yes", "0x5m_active-no")
        assert [m.condition_id for m in update.added_markets] == ["0x5m_active"]
        assert set(update.added_assets) == {"0x5m_active-yes", "0x5m_active-no"}


def test_auto_expire_sentinel_markets_after_window_close(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", poll_seconds=15, sentinel_enabled=True)

    # Market that ended 90 seconds ago (beyond SENTINEL_EXPIRY_GRACE_SECONDS = 60s)
    now = time.time()
    expired_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 90))
    active_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 200))

    expired_mkt = _sample_market_row("0xexpired", slug="btc-updown-5m-expired", end_date=expired_ts)
    active_mkt = _sample_market_row("0xactive", slug="btc-updown-5m-active", end_date=active_ts)

    state.markets = state._build([expired_mkt, active_mkt])
    state.asset_to_market = {t: m.condition_id for m in state.markets.values() for t in m.tokens}

    assert "0xexpired" in state.markets
    assert "0xactive" in state.markets

    # Run poll_incremental with empty new/closed rows from network
    with patch.object(state, "_fetch_incremental", return_value=([], [])):
        update = asyncio.run(state.poll_incremental())

        # Expired market must be automatically pruned!
        assert "0xexpired" not in state.markets
        assert "0xactive" in state.markets
        assert update is not None
        assert [m.condition_id for m in update.removed_markets] == ["0xexpired"]
        assert set(update.removed_assets) == {"0xexpired-yes", "0xexpired-no"}


def test_fast_clob_sweep_fallback(tmp_path):
    state = UniverseState(tmp_path / "manifest.json", max_pages=2)

    clob_batch = [
        {
            "condition_id": "0xclob1",
            "question": "Will BTC reach 100k?",
            "market_slug": "btc-reach-100k",
            "end_date_iso": "2027-01-01T00:00:00Z",
            "closed": False,
            "archived": False,
            "neg_risk": False,
            "rewards": {"rates": [{"rewards_daily_rate": 50}], "min_size": 100, "max_spread": 4.0},
            "tokens": [{"token_id": "tok_y", "outcome": "Yes"}, {"token_id": "tok_n", "outcome": "No"}],
            "volume_24hr": 5000,
        }
    ]

    with patch.object(state, "_get_json", return_value={"data": clob_batch, "next_cursor": None}), \
         patch.object(state, "_fetch_short_duration_markets", return_value=[]), \
         patch.object(state, "_fetch_deterministic_sentinels", return_value=[]):

        rows = state._fetch_markets()
        assert len(rows) == 1
        assert rows[0]["conditionId"] == "0xclob1"
        assert rows[0]["clobTokenIds"] == ["tok_y", "tok_n"]
        assert rows[0]["rewardsMinSize"] == 100
        assert rows[0]["rewardsMaxSpread"] == 4.0
