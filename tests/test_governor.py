"""Unit tests for PolySimulator RateLimitGovernor and budget enforcement."""

import time
from unittest.mock import patch

from polymarketbot.paper_trading.governor import BudgetConfig, RateLimitGovernor


def test_governor_min_interval_enforcement():
    cfg = BudgetConfig(min_request_interval_seconds=0.1)
    gov = RateLimitGovernor(config=cfg)

    # First request should not wait
    w1 = gov.acquire_read_slot()
    assert w1 == 0.0

    # Immediate second request should wait ~0.1s
    t0 = time.time()
    w2 = gov.acquire_read_slot()
    t1 = time.time()
    assert w2 > 0.0
    assert (t1 - t0) >= 0.08


def test_governor_order_write_cap():
    cfg = BudgetConfig(max_orders_per_day=3, min_request_interval_seconds=0.0)
    gov = RateLimitGovernor(config=cfg)

    # 3 allowed
    for _ in range(3):
        allowed, msg = gov.acquire_order_write_slot()
        assert allowed is True
        assert msg == "Permitted"

    # 4th blocked
    allowed, msg = gov.acquire_order_write_slot()
    assert allowed is False
    assert "daily limit reached" in msg


def test_governor_rpm_and_status():
    cfg = BudgetConfig(max_rpm=5, min_request_interval_seconds=0.0)
    gov = RateLimitGovernor(config=cfg)

    for _ in range(3):
        gov.acquire_read_slot()

    st = gov.get_status()
    assert st.requests_last_minute == 3
    assert st.requests_today == 3
    assert st.is_throttled is False


def test_governor_utc_day_rollover():
    cfg = BudgetConfig(max_orders_per_day=2, min_request_interval_seconds=0.0)
    gov = RateLimitGovernor(config=cfg)

    gov.acquire_order_write_slot()
    gov.acquire_order_write_slot()
    assert gov.get_status().orders_placed_today == 2

    # Simulate rollover to next day
    with patch("polymarketbot.paper_trading.governor.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-11-03"
        gov._sync_utc_day()
        st = gov.get_status()
        assert st.orders_placed_today == 0
        assert st.requests_today == 0
