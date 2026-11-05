"""Truth-layer tests for the live CLOB client (audit C2/C3/C4/C12/S17/M1/M3).

All tests are offline and deterministic: a fake session stands in for
``requests.Session`` and serves canned CLOB responses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import requests

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.trading.client import (
    NEG_RISK_CTF_EXCHANGE_V2,
    USDC_ASSET_ADDRESSES,
    ClobAPIError,
    ClobAuthCredentials,
    LiveClobClient,
    OrderPlacementResult,
    build_clob_hmac_signature,
    generate_order_salt,
)

SAMPLE_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
EOA_ADDRESS = "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"
PROXY_ADDRESS = "0x" + "11" * 20
API_KEY = "11111111-2222-3333-4444-555555555555"


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text="", json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = text or ("" if payload is None else json.dumps(payload))

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """Serves queued responses and records each call's kwargs."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        # DataAPIClient sets a default User-Agent on the session it is given.
        self.headers: dict[str, str] = {}

    def _next(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._next("DELETE", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)


def make_client(fake: FakeSession, *, signature_type: int = 1, address: str = PROXY_ADDRESS) -> LiveClobClient:
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key=API_KEY,
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            address=address,
            private_key=SAMPLE_PRIVATE_KEY,
            signature_type=signature_type,
        ),
        dry_run=False,
        order_version=2,
    )
    assert client.dry_run is False
    client._get_session = lambda: fake  # type: ignore[method-assign]
    return client


def make_dry_client() -> LiveClobClient:
    """Offline dry-run client with an explicit key (never reads the repo .env)."""
    client = LiveClobClient(
        credentials=ClobAuthCredentials(private_key=SAMPLE_PRIVATE_KEY),
        dry_run=True,
        order_version=2,
    )
    assert client.dry_run is True
    return client


def params_of(call: dict) -> dict:
    return dict(call.get("params") or {})


# --------------------------------------------------------------------------
# GET /data/orders — pagination, dict parsing, filters, failure semantics
# --------------------------------------------------------------------------
def test_open_orders_follows_cursor_and_parses_data_list():
    fake = FakeSession(
        FakeResponse({"limit": 2, "next_cursor": "MTAw", "count": 1, "data": [{"id": "a", "status": "LIVE"}]}),
        FakeResponse({"limit": 2, "next_cursor": "LTE=", "count": 1, "data": [{"id": "b", "status": "MATCHED"}]}),
    )
    client = make_client(fake)
    orders = client.get_open_orders()

    assert [o["id"] for o in orders] == ["a", "b"]
    assert [c["url"] for c in fake.calls] == [
        "https://clob.polymarket.com/data/orders",
        "https://clob.polymarket.com/data/orders",
    ]
    assert params_of(fake.calls[0])["next_cursor"] == "MA=="
    assert params_of(fake.calls[1])["next_cursor"] == "MTAw"


def test_open_orders_sends_market_filter():
    fake = FakeSession(FakeResponse({"next_cursor": "LTE=", "data": []}))
    client = make_client(fake)
    assert client.get_open_orders(market="0x" + "ab" * 32) == []
    assert params_of(fake.calls[0])["market"] == "0x" + "ab" * 32
    assert "id" not in params_of(fake.calls[0])


def test_open_orders_order_id_returns_order_regardless_of_status():
    fake = FakeSession(
        FakeResponse({"next_cursor": "LTE=", "data": [{"id": "0xdead", "status": "MATCHED"}]})
    )
    client = make_client(fake)
    order = client.get_order("0xdead")
    assert order is not None and order["status"] == "MATCHED"
    assert params_of(fake.calls[0])["id"] == "0xdead"
    assert len(fake.calls) == 1


def test_open_orders_stops_at_max_pages():
    fake = FakeSession(
        FakeResponse({"next_cursor": "a", "data": [{"id": "1"}]}),
        FakeResponse({"next_cursor": "b", "data": [{"id": "2"}]}),
        FakeResponse({"next_cursor": "c", "data": [{"id": "3"}]}),
    )
    client = make_client(fake)
    assert [o["id"] for o in client.get_open_orders(max_pages=2)] == ["1", "2"]
    assert len(fake.calls) == 2


def test_open_orders_repeated_cursor_raises():
    fake = FakeSession(
        FakeResponse({"next_cursor": "SAME", "data": [{"id": "1"}]}),
        FakeResponse({"next_cursor": "SAME", "data": [{"id": "2"}]}),
    )
    client = make_client(fake)
    with pytest.raises(ClobAPIError, match="repeated pagination cursor"):
        client.get_open_orders()


def test_open_orders_http_error_raises_not_empty_list():
    fake = FakeSession(FakeResponse({"error": "rate limited"}, status_code=429, text='{"error":"rate limited"}'))
    client = make_client(fake)
    with pytest.raises(ClobAPIError) as excinfo:
        client.get_open_orders()
    assert excinfo.value.status_code == 429
    assert "/data/orders" in str(excinfo.value)


def test_open_orders_network_error_raises():
    fake = FakeSession(requests.ConnectionError("connection reset"))
    client = make_client(fake)
    with pytest.raises(ClobAPIError) as excinfo:
        client.get_open_orders()
    assert excinfo.value.status_code == 0


def test_open_orders_unparseable_body_raises():
    fake = FakeSession(FakeResponse(status_code=200, text="<html>oops</html>", json_error=True))
    client = make_client(fake)
    with pytest.raises(ClobAPIError, match="unparseable"):
        client.get_open_orders()


def test_open_orders_wrong_shape_raises():
    fake = FakeSession(FakeResponse({"orders": []}))
    client = make_client(fake)
    with pytest.raises(ClobAPIError, match="data"):
        client.get_open_orders()


# --------------------------------------------------------------------------
# GET /data/trades
# --------------------------------------------------------------------------
def test_trades_sends_maker_address_and_paginates():
    fake = FakeSession(
        FakeResponse({"next_cursor": "MTAw", "data": [{"id": "t1", "size": "10"}]}),
        FakeResponse({"next_cursor": "LTE=", "data": [{"id": "t2", "size": "5"}]}),
    )
    client = make_client(fake)
    trades = client.get_trades(market="0x" + "cd" * 32, limit=50)
    assert [t["id"] for t in trades] == ["t1", "t2"]
    assert params_of(fake.calls[0])["maker_address"] == PROXY_ADDRESS
    assert params_of(fake.calls[0])["market"] == "0x" + "cd" * 32
    assert params_of(fake.calls[0])["limit"] == 50
    assert params_of(fake.calls[1])["next_cursor"] == "MTAw"


def test_trades_failure_raises():
    fake = FakeSession(FakeResponse({"error": "unavailable"}, status_code=503))
    client = make_client(fake)
    with pytest.raises(ClobAPIError):
        client.get_trades()


# --------------------------------------------------------------------------
# /balance-allowance
# --------------------------------------------------------------------------
def test_balance_request_uses_collateral_and_signature_type():
    fake = FakeSession(FakeResponse({"balance": "1500000", "allowances": {}}))
    client = make_client(fake)
    client.get_balance()
    params = params_of(fake.calls[0])
    assert params["asset_type"] == "COLLATERAL"
    assert params["signature_type"] == 1
    assert fake.calls[0]["url"].endswith("/balance-allowance")


def test_balance_parses_raw_base_units():
    fake = FakeSession(
        FakeResponse({"balance": "1500000", "allowances": {"0xexchange": "2500000"}})
    )
    client = make_client(fake)
    balance = client.get_balance()
    assert balance["available"] == pytest.approx(1.5)
    assert balance["allowance"] == pytest.approx(2.5)
    assert balance["locked"] == 0.0
    assert balance["raw"]["balance"] == "1500000"


def test_balance_parses_decimal_strings():
    fake = FakeSession(FakeResponse({"balance": "12.34", "allowance": "5.5"}))
    client = make_client(fake)
    balance = client.get_balance()
    assert balance["available"] == pytest.approx(12.34)
    assert balance["allowance"] == pytest.approx(5.5)


def test_allowances_approved_from_collateral_allowance():
    fake = FakeSession(FakeResponse({"balance": "0", "allowances": {"0xexchange": "1000000"}}))
    client = make_client(fake)
    allowances = client.get_allowances()
    assert allowances["approved"] is True
    assert allowances["collateral"] == pytest.approx(1.0)
    assert allowances["conditional"] == 0.0


def test_allowances_zero_allowance_is_not_approved():
    fake = FakeSession(FakeResponse({"balance": "0", "allowances": {"0xexchange": "0"}}))
    client = make_client(fake)
    assert client.get_allowances()["approved"] is False


def test_allowances_failure_raises_instead_of_false():
    fake = FakeSession(FakeResponse({"error": "boom"}, status_code=500))
    client = make_client(fake)
    with pytest.raises(ClobAPIError):
        client.get_allowances()


def test_allowances_missing_key_raises():
    fake = FakeSession(FakeResponse({"balance": "1000000"}))
    client = make_client(fake)
    with pytest.raises(ClobAPIError, match="allowance"):
        client.get_allowances()


def test_get_market_normalisation():
    fake = FakeSession(
        FakeResponse(
            {
                "condition_id": "0x" + "ef" * 32,
                "minimum_tick_size": 0.001,
                "neg_risk": True,
                "taker_base_fee": 20,
                "closed": False,
                "accepting_orders": True,
                "end_date_iso": "2027-01-01T00:00:00Z",
            }
        )
    )
    client = make_client(fake)
    market = client.get_market("0x" + "ef" * 32)
    assert fake.calls[0]["url"].endswith("/markets/" + "0x" + "ef" * 32)
    assert market["tick_size"] == pytest.approx(0.001)
    assert market["neg_risk"] is True
    assert market["fee_rate_bps"] == 20
    assert market["closed"] is False
    assert market["accepting_orders"] is True
    assert market["end_date_iso"] == "2027-01-01T00:00:00Z"
    assert market["raw"]["condition_id"] == "0x" + "ef" * 32


def test_get_market_defaults_missing_fields():
    fake = FakeSession(FakeResponse({"condition_id": "0xabc"}))
    client = make_client(fake)
    market = client.get_market("0xabc")
    assert market["tick_size"] == pytest.approx(0.01)
    assert market["fee_rate_bps"] == 0
    assert market["neg_risk"] is False
    assert market["end_date_iso"] is None


# --------------------------------------------------------------------------
# Reward telemetry parsing
# --------------------------------------------------------------------------
USDC_ADDRESS = sorted(USDC_ASSET_ADDRESSES)[0]
POL_ASSET_ADDRESS = "0x9c4E1703476E875070EE25b56A58B008CFb8FA78"


def test_daily_rewards_paginates_and_converts_asset_rates():
    fake = FakeSession(
        FakeResponse({
            "limit": 100,
            "count": 2,
            "next_cursor": "MTAw",
            "data": [
                {
                    "date": "2026-01-02T00:00:00Z",
                    "condition_id": "0xcond1",
                    "asset_address": USDC_ADDRESS,
                    "maker_address": PROXY_ADDRESS,
                    "earnings": 0.237519,
                    "asset_rate": 1,
                },
                {
                    "date": "2026-01-02T00:00:00Z",
                    "condition_id": "0xcond2",
                    "asset_address": POL_ASSET_ADDRESS,
                    "maker_address": PROXY_ADDRESS,
                    "earnings": 8.187219,
                    "asset_rate": 3.51,
                },
            ],
        }),
        FakeResponse({
            "limit": 100,
            "count": 1,
            "next_cursor": "LTE=",
            "data": [
                {
                    "date": "2026-01-02T00:00:00Z",
                    "condition_id": "0xcond1",
                    "asset_address": USDC_ADDRESS,
                    "maker_address": PROXY_ADDRESS,
                    "earnings": 0,
                    "asset_rate": 1,
                },
            ],
        }),
    )
    client = make_client(fake)
    daily = client.get_daily_rewards("2026-01-02")

    expected_cond2 = 8.187219 * 3.51
    assert daily["source"] == "clob_api"
    assert daily["date"] == "2026-01-02"
    assert daily["daily_rewards_usd"] == pytest.approx(0.237519 + expected_cond2)
    assert daily["daily_combined_usd"] == pytest.approx(daily["daily_rewards_usd"])
    assert daily["unknown_earnings"] == []
    assert daily["row_count"] == 3

    breakdown = {item["condition_id"]: item["earned_usd"] for item in daily["market_breakdown"]}
    assert breakdown["0xcond1"] == pytest.approx(0.237519)
    assert breakdown["0xcond2"] == pytest.approx(expected_cond2)

    assert [c["url"] for c in fake.calls] == [
        "https://clob.polymarket.com/rewards/user",
        "https://clob.polymarket.com/rewards/user",
    ]
    assert params_of(fake.calls[0])["date"] == "2026-01-02"
    assert params_of(fake.calls[0])["signature_type"] == 1
    assert params_of(fake.calls[0])["maker_address"] == PROXY_ADDRESS
    assert params_of(fake.calls[0])["next_cursor"] == "MA=="
    assert params_of(fake.calls[1])["next_cursor"] == "MTAw"


def test_daily_rewards_missing_rate_only_defaults_for_usdc():
    fake = FakeSession(FakeResponse({
        "limit": 100,
        "count": 2,
        "next_cursor": "LTE=",
        "data": [
            {
                "condition_id": "0xunknown",
                "asset_address": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "earnings": 5.0,
                # no asset_rate: not a USDC asset -> cannot be valued
            },
            {
                "condition_id": "0xusdc",
                "asset_address": USDC_ADDRESS,
                "earnings": 2.0,
                # no asset_rate: USDC -> 1:1
            },
        ],
    }))
    client = make_client(fake)
    daily = client.get_daily_rewards("2026-01-02")

    assert daily["daily_rewards_usd"] == pytest.approx(2.0)
    assert [item["condition_id"] for item in daily["market_breakdown"]] == ["0xusdc"]
    assert len(daily["unknown_earnings"]) == 1
    assert daily["unknown_earnings"][0]["condition_id"] == "0xunknown"


def test_daily_rewards_failure_is_not_reported_as_zero_earnings():
    fake = FakeSession(FakeResponse({"error": "boom"}, status_code=503))
    client = make_client(fake)
    daily = client.get_daily_rewards("2026-01-02")

    assert daily["source"] == "unavailable"
    assert daily["error"]
    assert daily["daily_rewards_usd"] == 0.0
    assert daily["market_breakdown"] == []


def test_total_rewards_uses_per_day_clob_array():
    fake = FakeSession(FakeResponse([
        {
            "date": "2026-01-02T00:00:00Z",
            "asset_address": USDC_ADDRESS,
            "maker_address": PROXY_ADDRESS,
            "earnings": 1.59984,
            "asset_rate": 1,
        },
        {
            "date": "2026-01-02T00:00:00Z",
            "asset_address": POL_ASSET_ADDRESS,
            "maker_address": PROXY_ADDRESS,
            "earnings": 8.187219,
            "asset_rate": 3.51,
        },
    ]))
    client = make_client(fake)
    total = client.get_total_rewards()

    expected = 1.59984 + 8.187219 * 3.51
    assert total["source"] == "clob_api_daily"
    assert total["total_rewards_usd"] == pytest.approx(expected)
    assert total["reward_income_usd"] == pytest.approx(expected)
    assert total["payout_count"] == 2
    assert total["unknown_earnings"] == []
    assert total["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    params = params_of(fake.calls[0])
    assert fake.calls[0]["url"].endswith("/rewards/user/total")
    assert params["date"] == total["date"]
    assert params["signature_type"] == 1
    assert params["maker_address"] == PROXY_ADDRESS


def test_total_rewards_falls_back_to_lifetime_data_api(monkeypatch):
    fake = FakeSession(FakeResponse({"error": "unavailable"}, status_code=503))
    client = make_client(fake)
    monkeypatch.setattr(
        DataAPIClient,
        "user_stats",
        lambda self, user: {"all_time_pnl": {"reward_income": 12.5, "maker_rebate": 1.5}},
    )
    monkeypatch.setattr(DataAPIClient, "activity", lambda self, **kwargs: [])

    total = client.get_total_rewards()
    assert total["source"] == "polymarket_data_api"
    assert total["total_rewards_usd"] == pytest.approx(14.0)
    assert total["reward_income_usd"] == pytest.approx(12.5)
    assert total["maker_rebates_usd"] == pytest.approx(1.5)


def test_total_rewards_unavailable_when_both_sources_fail(monkeypatch):
    fake = FakeSession(FakeResponse({"error": "unavailable"}, status_code=503))
    client = make_client(fake)

    def _boom(self, user):
        raise RuntimeError("data api down")

    monkeypatch.setattr(DataAPIClient, "user_stats", _boom)

    total = client.get_total_rewards()
    assert total["source"] == "unavailable"
    assert "data api down" in total["error"]
    assert total["total_rewards_usd"] == 0.0


def test_total_rewards_rejects_unexpected_object_body(monkeypatch):
    fake = FakeSession(FakeResponse({"total_rewards": 999.0}))
    client = make_client(fake)
    monkeypatch.setattr(DataAPIClient, "user_stats", lambda self, user: {})
    monkeypatch.setattr(DataAPIClient, "activity", lambda self, **kwargs: [])

    total = client.get_total_rewards()
    assert total["source"] == "polymarket_data_api"
    assert total["total_rewards_usd"] == 0.0


# --------------------------------------------------------------------------
# Signing identity (C12)
# --------------------------------------------------------------------------
def test_signed_order_identity_and_owner():
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key=API_KEY,
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            address=PROXY_ADDRESS,
            private_key=SAMPLE_PRIVATE_KEY,
            signature_type=1,
        ),
        dry_run=False,
        order_version=2,
    )
    fast_order, payload = client.build_signed_order(
        token_id="1234567890", side="BUY", price=0.52, size=50.0
    )

    assert client.maker_address.lower() == PROXY_ADDRESS.lower()
    assert client.signer_address.lower() == EOA_ADDRESS.lower()
    assert fast_order.maker.lower() == PROXY_ADDRESS.lower()
    assert fast_order.signer.lower() == EOA_ADDRESS.lower()
    assert fast_order.signatureType == 1
    assert payload["order"]["signatureType"] == 1
    # owner is the API key UUID, not an address
    assert payload["owner"] == API_KEY
    assert payload["order"]["signer"].lower() == EOA_ADDRESS.lower()
    assert payload["order"]["maker"].lower() == PROXY_ADDRESS.lower()

    headers = client._get_auth_headers("GET", "/data/orders")
    # L2 auth identifies the API-key owner (the signing EOA), never the funder.
    assert headers["POLY_ADDRESS"].lower() == EOA_ADDRESS.lower()
    assert headers["POLY_API_KEY"] == API_KEY


@pytest.mark.parametrize(
    ("signature_type", "funder"),
    [(0, ""), (1, PROXY_ADDRESS), (2, PROXY_ADDRESS)],
)
def test_auth_header_is_signer_eoa_for_every_signature_type(signature_type, funder):
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key=API_KEY,
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            address=funder,
            private_key=SAMPLE_PRIVATE_KEY,
            signature_type=signature_type,
        ),
        dry_run=False,
        order_version=2,
    )
    fast_order, payload = client.build_signed_order("42", "BUY", 0.5, 10.0, salt=3)

    expected_maker = funder or EOA_ADDRESS
    assert client.signer_address.lower() == EOA_ADDRESS.lower()
    assert client.maker_address.lower() == expected_maker.lower()

    headers = client._get_auth_headers("GET", "/data/orders")
    assert headers["POLY_ADDRESS"].lower() == client.signer_address.lower()
    assert headers["POLY_ADDRESS"].lower() != client.maker_address.lower() or not funder
    assert headers["POLY_API_KEY"] == API_KEY

    assert fast_order.maker.lower() == expected_maker.lower()
    assert fast_order.signer.lower() == EOA_ADDRESS.lower()
    assert payload["order"]["maker"].lower() == expected_maker.lower()
    assert payload["order"]["signer"].lower() == EOA_ADDRESS.lower()
    assert fast_order.signatureType == signature_type
    assert payload["owner"] == API_KEY


def test_signature_type_zero_makes_eoa_the_maker():
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key=API_KEY,
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            private_key=SAMPLE_PRIVATE_KEY,
        ),
        dry_run=False,
        order_version=2,
    )
    fast_order, _ = client.build_signed_order("42", "SELL", 0.4, 10.0)
    assert client.signature_type == 0
    assert client.maker_address.lower() == EOA_ADDRESS.lower()
    assert fast_order.maker.lower() == EOA_ADDRESS.lower()
    assert fast_order.signatureType == 0


def test_neg_risk_uses_separate_cached_signer():
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key=API_KEY,
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            private_key=SAMPLE_PRIVATE_KEY,
        ),
        dry_run=False,
        order_version=2,
    )
    _, plain = client.build_signed_order("42", "BUY", 0.5, 10.0, salt=7)
    _, neg = client.build_signed_order("42", "BUY", 0.5, 10.0, salt=7, neg_risk=True)

    assert plain["order"]["signature"] != neg["order"]["signature"]
    negative_key = f"v2:{NEG_RISK_CTF_EXCHANGE_V2.lower()}"
    negative_signer = client._signers[negative_key]
    assert negative_signer.verifying_contract == NEG_RISK_CTF_EXCHANGE_V2
    assert client._signers[negative_key] is negative_signer


def test_order_salt_is_random_and_unique():
    salts = [generate_order_salt() for _ in range(2000)]
    assert len(set(salts)) == len(salts)
    assert all(0 < s < (1 << 63) for s in salts)

    client = make_dry_client()
    first, _ = client.build_signed_order("42", "BUY", 0.5, 1.0)
    second, _ = client.build_signed_order("42", "BUY", 0.5, 1.0)
    assert first.salt != second.salt


# --------------------------------------------------------------------------
# place_order validation + tick rounding
# --------------------------------------------------------------------------
def test_place_order_rejects_non_numeric_token_id():
    client = make_dry_client()
    result = client.place_order(token_id="YES_TOKEN", side="BUY", price=0.5, size=10.0)
    assert isinstance(result, OrderPlacementResult)
    assert result.success is False
    assert "token_id" in (result.error_message or "")

    with pytest.raises(ValueError, match="token_id"):
        client.build_signed_order("YES_TOKEN", "BUY", 0.5, 10.0)


def test_place_order_rejects_out_of_range_price_and_size():
    client = make_dry_client()
    assert client.place_order("42", "BUY", 1.2, 10.0).success is False
    assert client.place_order("42", "BUY", 0.0, 10.0).success is False
    assert client.place_order("42", "BUY", 0.5, 0.0).success is False
    assert client.place_order("42", "HOLD", 0.5, 10.0).success is False


def test_place_order_rounds_price_to_tick_and_size_to_6dp():
    fake = FakeSession(FakeResponse({"success": True, "orderID": "0xabc"}))
    client = make_client(fake)
    result = client.place_order(
        "42",
        "BUY",
        0.413,
        10.1234567,
        tick_size=0.01,
        fee_rate_bps=30,
        neg_risk=False,
    )

    assert result.success is True
    assert result.order_id == "0xabc"
    assert result.price == pytest.approx(0.41)
    # size is floored to 2 dp per the tick rounding table
    assert result.size == pytest.approx(10.12)

    posted = json.loads(fake.calls[0]["data"])
    assert fake.calls[0]["url"].endswith("/order")
    assert posted["owner"] == API_KEY
    assert posted["order"]["signatureType"] == 1
    # V2 signed struct has no feeRateBps; makerAmount = 10.12 * 0.41 = 4.1492
    assert "feeRateBps" not in posted["order"]
    assert int(posted["order"]["makerAmount"]) == 4_149_200
    assert int(posted["order"]["takerAmount"]) == 10_120_000


def test_place_order_rejection_returns_failure_not_success():
    fake = FakeSession(FakeResponse({"success": False, "errorMsg": "not enough balance"}))
    client = make_client(fake)
    result = client.place_order("42", "BUY", 0.5, 10.0)
    assert result.success is False
    assert result.status == "REJECTED"
    assert "not enough balance" in (result.error_message or "")


def test_place_order_network_error_returns_failure():
    fake = FakeSession(requests.Timeout("timed out"))
    client = make_client(fake)
    result = client.place_order("42", "BUY", 0.5, 10.0, tick_size=0.01, neg_risk=False)
    assert result.success is False
    assert result.status == "NETWORK_ERROR"


def test_dry_run_place_order_records_mock_and_keeps_balance():
    client = make_dry_client()
    result = client.place_order("42", "SELL", 0.5, 10.0)
    assert result.success is True and result.is_dry_run is True
    assert len(client.get_open_orders()) == 1
    assert client.cancel_order(result.order_id) is True
    assert client.get_open_orders() == []
    assert client.get_balance()["available"] == pytest.approx(100.0)


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------
def test_public_names_still_import():
    import polymarketbot.trading.client as module

    assert module.FastOrderSigner is not None
    assert module.DEFAULT_CLOB_HOST == "https://clob.polymarket.com"
    assert callable(build_clob_hmac_signature)
    assert callable(module.LiveClobClient.get_open_orders)
