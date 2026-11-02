"""Unit tests for PolySimulator client and trader."""

from unittest.mock import MagicMock, patch
import pytest
import requests

from polymarketbot.paper_trading.polysim_client import (
    PolySimBalance,
    PolySimOrderResult,
    PolySimulatorClient,
)
from polymarketbot.paper_trading.polysim_trader import PolySimulatorTrader


def test_polysim_balance_retrieval():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "balance": "100.00",
        "currency": "USD",
        "unrealized_pnl": "0.00",
        "total_value": "100.00",
        "starting_balance": "100.00",
    }
    
    mock_session = MagicMock()
    mock_session.request.return_value = mock_resp
    
    client = PolySimulatorClient(api_key="ps_live_test123", session=mock_session)
    bal = client.get_balance()
    
    assert isinstance(bal, PolySimBalance)
    assert bal.balance == 100.00
    assert bal.currency == "USD"
    assert bal.total_value == 100.00


def test_polysim_order_placement_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"success": true}'
    mock_resp.json.return_value = {
        "success": True,
        "orderID": "999888",
        "status": "live",
        "price": "0.4500",
        "size": "50.0000",
        "side": "BUY",
    }
    
    mock_session = MagicMock()
    mock_session.request.return_value = mock_resp
    
    client = PolySimulatorClient(api_key="ps_live_test123", session=mock_session)
    res = client.place_order(
        token_id="tok_123",
        side="BUY",
        size=50.0,
        price=0.45,
        tick_size=0.01,
    )
    
    assert res.success is True
    assert res.order_id == "999888"
    assert res.status == "live"
    assert res.price == 0.45
    assert res.size == 50.0


def test_polysim_order_placement_rejected():
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = '{"error": "INSUFFICIENT_BALANCE", "errorMsg": "Not enough funds"}'
    mock_resp.json.return_value = {
        "error": "INSUFFICIENT_BALANCE",
        "errorMsg": "Not enough funds",
    }
    
    mock_session = MagicMock()
    mock_session.request.return_value = mock_resp
    
    client = PolySimulatorClient(api_key="ps_live_test123", session=mock_session)
    res = client.place_order(
        token_id="tok_123",
        side="BUY",
        size=5000.0,
        price=0.45,
    )
    
    assert res.success is False
    assert res.order_id is None
    assert res.status == "rejected"
    assert "Not enough funds" in (res.error_msg or "")


def test_polysim_cancel_order():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"order_id": 999888, "status": "CANCELLED"}
    
    mock_session = MagicMock()
    mock_session.request.return_value = mock_resp
    
    client = PolySimulatorClient(api_key="ps_live_test123", session=mock_session)
    assert client.cancel_order("999888") is True


def test_polysim_binary_quote_calculation():
    client = MagicMock()
    trader = PolySimulatorTrader(
        client=client,
        quote_size=50.0,
        spread_offset_cents=0.75,
    )
    trader.target_tick_size = 0.01

    # At mid = 0.50, half_spread = 0.0075 ->
    # raw yes_bid = 0.50 - 0.0075 = 0.4925 -> quantized 0.49
    # raw no_bid = (1.00 - 0.50) - 0.0075 = 0.4925 -> quantized 0.49
    yes_bid, no_bid = trader.calculate_binary_quotes(0.50)
    assert yes_bid == 0.49
    assert no_bid == 0.49

    # At mid = 0.30:
    # raw yes_bid = 0.30 - 0.0075 = 0.2925 -> quantized 0.29
    # raw no_bid = (1.00 - 0.30) - 0.0075 = 0.6925 -> quantized 0.69
    yes_bid, no_bid = trader.calculate_binary_quotes(0.30)
    assert yes_bid == 0.29
    assert no_bid == 0.69


def test_polysim_database(tmp_path):
    from polymarketbot.paper_trading.polysim_db import PolySimDatabase

    db = PolySimDatabase(db_path=tmp_path / "test_polysim.db")
    
    # Record order
    db.record_order(
        order_id="ord_101",
        token_id="tok_yes",
        side="BUY",
        price=0.35,
        size=50.0,
        status="live",
        outcome="Yes",
    )
    live_orders = db.get_live_orders()
    assert len(live_orders) == 1
    assert live_orders[0]["order_id"] == "ord_101"

    # Update order
    db.update_order_status("ord_101", "matched")
    assert len(db.get_live_orders()) == 0

    # Record reward epoch
    db.record_reward_epoch(
        market_id="mkt_1",
        token_id="tok_yes",
        mid_price=0.35,
        spread_offset_cents=0.75,
        max_spread_cents=3.0,
        quote_size=100.0,
        user_score=56.25,
        competing_score=400.0,
        daily_reward_pool=2000.0,
        epoch_reward_usd=0.1714,
    )
    daily = db.get_daily_summary()
    assert daily is not None
    assert daily["total_epochs"] == 1
    assert pytest.approx(daily["accumulated_reward_usd"], rel=1e-3) == 0.1714


def test_polysim_rate_limit_retry():
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "0.01"}
    
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"balance": "100.00"}
    
    mock_session = MagicMock()
    mock_session.request.side_effect = [resp_429, resp_200]
    
    client = PolySimulatorClient(api_key="ps_live_test123", session=mock_session)
    with patch("time.sleep", return_value=None):
        resp = client._request("GET", "/account/balance")
        assert resp.status_code == 200
        assert mock_session.request.call_count == 2

