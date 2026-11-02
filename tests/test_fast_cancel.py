"""Tests for Dedicated Fast Cancellation Transport."""

import http.client
import socket
from unittest.mock import MagicMock, call, patch

import pytest

from polymarketbot.execution.fast_cancel import (
    CancelResult,
    DedicatedCancellationPool,
)


@pytest.fixture
def mock_https_conn():
    """Mock HTTPSConnection in fast_cancel module."""
    with patch("polymarketbot.execution.fast_cancel.http.client.HTTPSConnection") as mock_cls:
        conn = MagicMock()
        mock_sock = MagicMock()
        conn.sock = mock_sock

        # Default response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status":"cancelled"}'
        conn.getresponse.return_value = mock_resp

        mock_cls.return_value = conn
        yield mock_cls, conn, mock_sock


def test_pool_pre_warming_and_socket_options(mock_https_conn):
    """Verify pool pre-warms connections and applies TCP_NODELAY and SO_KEEPALIVE."""
    mock_cls, conn, mock_sock = mock_https_conn

    pool = DedicatedCancellationPool(
        host="clob.polymarket.com",
        port=443,
        pool_size=3,
        auto_warm=True,
    )

    assert mock_cls.call_count == 3
    assert conn.connect.call_count == 3

    # Check socket options set on each connection
    expected_calls = [
        call(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]
    mock_sock.setsockopt.assert_has_calls(expected_calls, any_order=False)

    pool.close()


def test_fast_cancel_order_minimal_serialization(mock_https_conn):
    """Verify cancel_order serializes directly to ASCII bytes and calls DELETE /order."""
    mock_cls, conn, mock_sock = mock_https_conn

    pool = DedicatedCancellationPool(pool_size=1, auto_warm=True)
    res = pool.cancel_order("0xorder_abc_123")

    assert res.success is True
    assert res.status == 200
    assert res.order_id == "0xorder_abc_123"
    assert res.endpoint == "/order"
    assert "cancelled" in res.body

    # Check request called with exact ASCII byte payload
    conn.request.assert_called_with(
        "DELETE",
        "/order",
        body=b'{"orderID":"0xorder_abc_123"}',
        headers={
            "Host": "clob.polymarket.com",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
        },
    )

    pool.close()


def test_fast_cancel_all_serialization(mock_https_conn):
    """Verify cancel_all sends appropriate body for market-specific or global cancel."""
    mock_cls, conn, mock_sock = mock_https_conn

    pool = DedicatedCancellationPool(pool_size=1, auto_warm=True)

    # Scoped to market
    res_market = pool.cancel_all(market="0xcondition_xyz")
    assert res_market.success is True
    conn.request.assert_called_with(
        "DELETE",
        "/cancel-all",
        body=b'{"market":"0xcondition_xyz"}',
        headers={
            "Host": "clob.polymarket.com",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
        },
    )

    # Global
    res_global = pool.cancel_all()
    assert res_global.success is True
    conn.request.assert_called_with(
        "DELETE",
        "/cancel-all",
        body=b"{}",
        headers={
            "Host": "clob.polymarket.com",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
        },
    )

    pool.close()


def test_connection_drop_reconnect_and_requeue(mock_https_conn):
    """Verify that if a persistent connection drops, the pool reconnects and retries."""
    mock_cls, conn, mock_sock = mock_https_conn

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"success":true}'

    # First request raises RemoteDisconnected, retry succeeds
    conn.request.side_effect = [
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        None,
    ]
    conn.getresponse.return_value = mock_resp

    pool = DedicatedCancellationPool(pool_size=1, max_retries=2, auto_warm=True)
    res = pool.cancel_order("0xretry_order")

    assert res.success is True
    assert res.status == 200
    assert conn.request.call_count == 2

    pool.close()


def test_cancel_orders_batch(mock_https_conn):
    """Verify batch cancellation of multiple orders."""
    mock_cls, conn, mock_sock = mock_https_conn

    pool = DedicatedCancellationPool(pool_size=2, auto_warm=True)
    order_ids = ["0x1", "0x2", "0x3"]
    results = pool.cancel_orders(order_ids)

    assert len(results) == 3
    for idx, r in enumerate(results):
        assert r.success is True
        assert r.order_id == order_ids[idx]

    pool.close()


def test_context_manager(mock_https_conn):
    """Verify context manager closes connections cleanly upon exit."""
    mock_cls, conn, mock_sock = mock_https_conn

    with DedicatedCancellationPool(pool_size=2, auto_warm=True) as pool:
        assert not pool._is_closed

    assert pool._is_closed
