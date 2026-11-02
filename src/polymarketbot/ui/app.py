"""Streamlit UI for PolySimulator Cloud Paper Trading & Polymarket Liquidity Rewards.

Provides real-time interactive monitoring of:
- PolySimulator simulated account ($100 API wallet, equity, positions)
- Live Polymarket order book depth and book walking from PolySimulator
- Active two-sided complementary maker quotes (BUY YES + BUY NO)
- SQLite quadratic liquidity rewards scoring (1,440 daily epochs, $1.00 payout floor)
- Emergency kill-switch and manual order management
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
import pandas as pd
import streamlit as st

from polymarketbot.paper_trading.polysim_client import PolySimulatorClient
from polymarketbot.paper_trading.polysim_db import PolySimDatabase
from polymarketbot.paper_trading.rewards import (
    compute_competing_depth_score,
    compute_quadratic_score,
)

# -----------------------------------------------------------------------------
# Streamlit App Configuration
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="PolySimulator MM & Rewards Terminal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for high-density trading terminal styling
st.markdown(
    """
    <style>
    .metric-card {
        background-color: #1e222d;
        border: 1px solid #2a2e39;
        border-radius: 8px;
        padding: 14px;
        text-align: center;
    }
    .metric-title {
        color: #848e9c;
        font-size: 13px;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .metric-value {
        color: #f0f3f6;
        font-size: 24px;
        font-weight: 700;
        margin-top: 4px;
    }
    .metric-delta-pos {
        color: #00c087;
        font-size: 13px;
        font-weight: 600;
    }
    .metric-delta-neg {
        color: #f6465d;
        font-size: 13px;
        font-weight: 600;
    }
    .order-bid {
        background-color: rgba(0, 192, 135, 0.12);
        color: #00c087;
        font-weight: 600;
        padding: 3px 8px;
        border-radius: 4px;
    }
    .order-ask {
        background-color: rgba(246, 70, 93, 0.12);
        color: #f6465d;
        font-weight: 600;
        padding: 3px 8px;
        border-radius: 4px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_client() -> PolySimulatorClient:
    return PolySimulatorClient()


@st.cache_resource
def get_db() -> PolySimDatabase:
    return PolySimDatabase()


client = get_client()
db = get_db()

# -----------------------------------------------------------------------------
# Sidebar: Connection, Controls & Settings
# -----------------------------------------------------------------------------
st.sidebar.title("🎮 PolySimulator Bot")

if not client.api_key:
    st.sidebar.error("⚠️ POLYSIMULATOR_API_KEY is not configured in .env!")
    st.stop()

# Auto-refresh mechanism
auto_refresh = st.sidebar.checkbox("Auto-refresh UI", value=True)
refresh_interval = st.sidebar.slider("Refresh interval (seconds)", min_value=3, max_value=30, value=5)

st.sidebar.markdown("---")
st.sidebar.subheader("⚙️ Quoting Parameters")
quote_size = st.sidebar.slider("Quote Size (shares)", min_value=10.0, max_value=200.0, value=50.0, step=10.0)
spread_offset = st.sidebar.slider("Spread Offset (cents)", min_value=0.25, max_value=2.50, value=0.75, step=0.05)
max_spread_v = st.sidebar.slider("Max Spread v (cents)", min_value=1.0, max_value=5.0, value=3.0, step=0.5)
daily_reward_pool = st.sidebar.number_input("Daily Reward Pool ($/day)", min_value=100.0, max_value=10000.0, value=2000.0, step=500.0)

st.sidebar.markdown("---")
st.sidebar.subheader("⚡ Quick Actions")

# Fetch markets for dropdown
try:
    markets = client.get_markets(limit=15, hot_only=True)
    if not markets:
        markets = client.get_markets(limit=15)
except Exception as e:
    st.sidebar.error(f"Failed to fetch markets: {e}")
    markets = []

valid_markets = [
    m for m in markets
    if not m.get("closed") and len(m.get("clob_token_ids", [])) >= 2
]

market_options = {
    m["question"]: m for m in valid_markets
}

if not market_options:
    st.warning("No active binary markets found on PolySimulator.")
    st.stop()

selected_question = st.sidebar.selectbox(
    "Target Market",
    options=list(market_options.keys()),
    index=0,
)
selected_market = market_options[selected_question]
yes_token = selected_market["clob_token_ids"][0]
no_token = selected_market["clob_token_ids"][1]
cond_id = selected_market.get("condition_id", "")
tick_size = client.get_tick_size(yes_token)

# -----------------------------------------------------------------------------
# Action Handlers: Place Quotes / Cancel All
# -----------------------------------------------------------------------------
col_act1, col_act2 = st.sidebar.columns(2)

if col_act1.button("🚀 Quote Both Sides", use_container_width=True, type="primary"):
    with st.spinner("Calculating and posting complementary quotes..."):
        try:
            # 1. Fetch current midpoint
            mid = client.get_midpoint(yes_token)
            if not mid or mid <= 0:
                book = client.get_book(yes_token, depth=5)
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    mid = (float(bids[-1]["price"]) + float(asks[-1]["price"])) / 2.0
                else:
                    mid = 0.50

            # 2. Binary complementation pricing
            half_spread = spread_offset / 100.0
            raw_yes_bid = max(0.01, mid - half_spread)
            raw_no_bid = max(0.01, (1.0 - mid) - half_spread)
            yes_bid = round(round(raw_yes_bid / tick_size) * tick_size, 4)
            no_bid = round(round(raw_no_bid / tick_size) * tick_size, 4)

            # 3. Post BUY YES
            r_yes = client.place_order(
                token_id=yes_token,
                side="BUY",
                size=quote_size,
                price=yes_bid,
                order_type="GTC",
                tick_size=tick_size,
            )
            if r_yes.success and r_yes.order_id:
                db.record_order(
                    order_id=r_yes.order_id,
                    token_id=yes_token,
                    side="BUY",
                    price=yes_bid,
                    size=quote_size,
                    status="live",
                    market_id=cond_id,
                    outcome="Yes",
                )

            time.sleep(0.5)

            # 4. Post BUY NO
            r_no = client.place_order(
                token_id=no_token,
                side="BUY",
                size=quote_size,
                price=no_bid,
                order_type="GTC",
                tick_size=tick_size,
            )
            if r_no.success and r_no.order_id:
                db.record_order(
                    order_id=r_no.order_id,
                    token_id=no_token,
                    side="BUY",
                    price=no_bid,
                    size=quote_size,
                    status="live",
                    market_id=cond_id,
                    outcome="No",
                )

            st.sidebar.success(f"Quoted: YES @ ${yes_bid:.3f} | NO @ ${no_bid:.3f}")
        except Exception as exc:
            st.sidebar.error(f"Quoting failed: {exc}")

if col_act2.button("🛑 Cancel All", use_container_width=True):
    with st.spinner("Cancelling all resting orders..."):
        res = client.cancel_all()
        # Mark local orders as cancelled
        for o in db.get_live_orders():
            db.update_order_status(o["order_id"], "cancelled")
        st.sidebar.warning("All orders cancelled.")

# -----------------------------------------------------------------------------
# Main Top Header: Account Balance & Platform State
# -----------------------------------------------------------------------------
st.title("📊 PolySimulator Paper Trading & Rewards Terminal")

try:
    bal = client.get_balance()
except Exception as e:
    st.error(f"Error reading account balance from PolySimulator: {e}")
    st.stop()

# Header Metrics
m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    st.metric("API Cash Balance", f"${bal.balance:.2f} {bal.currency}")
with m2:
    st.metric("Total Equity", f"${bal.total_value:.2f}", delta=f"{bal.total_value - bal.starting_balance:+.2f}")
with m3:
    st.metric("Unrealized PnL", f"${bal.unrealized_pnl:.2f}")
with m4:
    st.metric("Sandbox Bankroll", f"${bal.starting_balance:.2f}")
with m5:
    cum_payout = db.get_total_cumulative_rewards()
    st.metric("Rewards Earned (SQLite)", f"${cum_payout:.2f}", delta="Clearing $1/day")

st.markdown("---")

# -----------------------------------------------------------------------------
# Tabs: 1. Order Book & Quotes | 2. Liquidity Rewards | 3. Orders & Positions
# -----------------------------------------------------------------------------
tab_book, tab_rewards, tab_history = st.tabs([
    "📖 Live Book & Active Quotes",
    "🏆 Liquidity Rewards Tracker",
    "📑 Orders, Fills & Positions",
])

# =============================================================================
# TAB 1: Live Order Book & Active Quotes
# =============================================================================
with tab_book:
    st.subheader(f"Market: {selected_question}")
    st.caption(f"Condition ID: `{cond_id}` | Tick: `{tick_size}` | YES Token: `{yes_token[:20]}...`")

    col_book, col_quotes = st.columns([3, 2])

    with col_book:
        st.markdown("#### PolySimulator L2 Order Book (YES Token)")
        try:
            book_data = client.get_book(yes_token, depth=15)
            raw_bids = book_data.get("bids", [])
            raw_asks = book_data.get("asks", [])

            # Format bids and asks
            # PolySimulator sorts bids ascending (best bid at [-1]), asks descending (best ask at [-1])
            formatted_bids = []
            for b in reversed(raw_bids):
                p = float(b["price"])
                s = float(b["size"])
                formatted_bids.append({"Side": "BID", "Price ($)": f"{p:.3f}", "Size": f"{s:.1f}", "Total ($)": f"{p * s:.2f}"})

            formatted_asks = []
            for a in reversed(raw_asks):
                p = float(a["price"])
                s = float(a["size"])
                formatted_asks.append({"Side": "ASK", "Price ($)": f"{p:.3f}", "Size": f"{s:.1f}", "Total ($)": f"{p * s:.2f}"})

            col_b, col_a = st.columns(2)
            with col_b:
                st.markdown("**Bids (Green)**")
                if formatted_bids:
                    st.dataframe(pd.DataFrame(formatted_bids), hide_index=True, use_container_width=True)
                else:
                    st.info("No bids on book.")
            with col_a:
                st.markdown("**Asks (Red)**")
                if formatted_asks:
                    st.dataframe(pd.DataFrame(formatted_asks), hide_index=True, use_container_width=True)
                else:
                    st.info("No asks on book.")

        except Exception as e:
            st.error(f"Failed to fetch order book: {e}")

    with col_quotes:
        st.markdown("#### Live Resting Quotes on PolySimulator")
        try:
            open_orders = client.get_open_orders()
            if open_orders:
                orders_view = []
                for o in open_orders:
                    oid = str(o.get("order_id") or o.get("id"))
                    side = o.get("side", "")
                    price = float(o.get("limit_price") or o.get("price") or 0.0)
                    size = float(o.get("quantity") or o.get("size") or 0.0)
                    outcome = o.get("outcome", "")
                    status = o.get("status", "PENDING")
                    orders_view.append({
                        "ID": oid,
                        "Outcome": outcome,
                        "Side": side,
                        "Price": f"${price:.3f}",
                        "Size": f"{size:.0f}",
                        "Status": status,
                    })
                st.dataframe(pd.DataFrame(orders_view), hide_index=True, use_container_width=True)
            else:
                st.info("No active resting orders on PolySimulator. Click 'Quote Both Sides' in the sidebar.")
        except Exception as e:
            st.error(f"Failed to fetch open orders: {e}")

        # Midpoint & Spread Info Card
        mid_val = client.get_midpoint(yes_token) or 0.50
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Current Midpoint & Spread</div>
                <div class="metric-value">${mid_val:.3f}</div>
                <div style="color: #848e9c; font-size: 13px; margin-top: 6px;">
                    Target YES Bid: <b>${max(0.01, mid_val - spread_offset/100.0):.3f}</b> | 
                    Target NO Bid: <b>${max(0.01, (1.0 - mid_val) - spread_offset/100.0):.3f}</b>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

# =============================================================================
# TAB 2: Liquidity Rewards Tracker (SQLite Engine)
# =============================================================================
with tab_rewards:
    st.subheader("🏆 Polymarket Liquidity Rewards Tracker (SQLite)")
    st.markdown(
        """
        PolySimulator executes orders against live Polymarket depth but does not natively accrue rewards.
        Our local SQLite engine tracks **1-minute sampling epochs** against qualifying depth using Polymarket's
        official quadratic scoring rule: $S(v, s) = ((v - s)/v)^2 \\times \\text{size}$.
        """
    )

    daily_summary = db.get_daily_summary()
    accrued_today = daily_summary.get("accumulated_reward_usd", 0.0) if daily_summary else 0.0
    payout_today = daily_summary.get("payout_usd", 0.0) if daily_summary else 0.0
    cleared_floor = bool(daily_summary.get("cleared_floor", 0)) if daily_summary else False
    epochs_sampled = daily_summary.get("total_epochs", 0) if daily_summary else 0

    rc1, rc2, rc3, rc4 = st.columns(4)
    with rc1:
        st.metric("Today's Accrued Rewards", f"${accrued_today:.4f}")
    with rc2:
        st.metric("Daily Payout ($1 Floor)", f"${payout_today:.2f}", delta="Cleared" if cleared_floor else "Pending Floor")
    with rc3:
        st.metric("Sampling Epochs (1/min)", f"{epochs_sampled} / 1,440")
    with rc4:
        user_sc = compute_quadratic_score(max_spread_v, spread_offset, quote_size * 2)
        st.metric("Your Quote Score S(v,s)", f"{user_sc:.1f} pts")

    # Payout Floor Progress Bar
    floor_pct = min(1.0, accrued_today / 1.0)
    st.write(f"**Progress towards $1.00 Polymarket Daily Payout Floor:** `${accrued_today:.3f}` / `$1.000` ({floor_pct*100:.1f}%)")
    st.progress(floor_pct)

    # Epoch Historical Chart
    with db._get_connection() as conn:
        df_epochs = pd.read_sql_query(
            "SELECT timestamp, epoch_reward_usd, user_score, competing_score FROM reward_epochs ORDER BY timestamp DESC LIMIT 100",
            conn,
        )

    if not df_epochs.empty:
        df_epochs["time"] = pd.to_datetime(df_epochs["timestamp"], unit="s")
        st.markdown("#### Reward Payout History (Per 1-Minute Epoch)")
        st.line_chart(df_epochs.set_index("time")[["epoch_reward_usd"]], height=260)

        st.markdown("#### User Score vs Competing Depth Score")
        st.line_chart(df_epochs.set_index("time")[["user_score", "competing_score"]], height=260)
    else:
        st.info("No reward epochs recorded in SQLite yet. As the bot runs, 1-minute snapshots appear here.")

# =============================================================================
# TAB 3: Orders, Fills & Positions
# =============================================================================
with tab_history:
    st.subheader("📑 Order History, Database Audit & Positions")

    col_pos, col_ord = st.columns(2)

    with col_pos:
        st.markdown("#### Open Simulated Positions (PolySimulator)")
        try:
            positions = client.get_positions(status="open")
            if positions:
                st.dataframe(pd.DataFrame(positions), hide_index=True, use_container_width=True)
            else:
                st.info("No open token positions currently held.")
        except Exception as e:
            st.error(f"Error fetching positions: {e}")

    with col_ord:
        st.markdown("#### Recent SQLite Logged Orders")
        with db._get_connection() as conn:
            df_orders = pd.read_sql_query(
                "SELECT order_id, outcome, side, price, size, status, placed_at FROM orders ORDER BY placed_at DESC LIMIT 25",
                conn,
            )
        if not df_orders.empty:
            df_orders["placed_at"] = pd.to_datetime(df_orders["placed_at"], unit="s")
            st.dataframe(df_orders, hide_index=True, use_container_width=True)
        else:
            st.info("No orders logged in SQLite yet.")

# -----------------------------------------------------------------------------
# Auto-refresh Timer
# -----------------------------------------------------------------------------
if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
