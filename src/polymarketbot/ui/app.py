"""Executive Telemetry & Analytics Terminal for Autonomous Liquidity Rewards Harvester.

Viewer & Analytics Dashboard for the fully autonomous market maker:
- The bot autonomously scans the market universe, ranks net yields, and selects markets.
- The bot autonomously sizes quote shares (Q*) and decides single vs multi-market allocation.
- The ONLY user-configurable parameter is `unlocked_capital_usd` ($100 default).
- Displays real-time executive analytics: rewards progress toward $1.00 floor, live book depth,
  bot's active quotes, algorithmic decision reasoning, and performance graphs.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
import pandas as pd
import streamlit as st

from polymarketbot.paper_trading.autonomous_daemon import AutonomousHarvesterDaemon
from polymarketbot.paper_trading.polysim_client import PolySimulatorClient
from polymarketbot.paper_trading.polysim_db import PolySimDatabase

# -----------------------------------------------------------------------------
# Streamlit App Configuration
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Autonomous Rewards Harvester Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .terminal-card {
        background-color: #1a1e29;
        border: 1px solid #282f3f;
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 12px;
    }
    .metric-header {
        color: #8b949e;
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.6px;
    }
    .metric-number {
        color: #f0f6fc;
        font-size: 26px;
        font-weight: 700;
        margin-top: 4px;
    }
    .status-running {
        background-color: rgba(35, 134, 54, 0.2);
        color: #3fb950;
        border: 1px solid #238636;
        padding: 4px 10px;
        border-radius: 20px;
        font-weight: 600;
        font-size: 13px;
        display: inline-block;
    }
    .status-paused {
        background-color: rgba(218, 54, 51, 0.2);
        color: #f85149;
        border: 1px solid #da3633;
        padding: 4px 10px;
        border-radius: 20px;
        font-weight: 600;
        font-size: 13px;
        display: inline-block;
    }
    .decision-box {
        background-color: #131722;
        border-left: 4px solid #58a6ff;
        padding: 12px 16px;
        border-radius: 0 8px 8px 0;
        margin: 10px 0;
        font-family: monospace;
        font-size: 13px;
        color: #e6edf3;
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
# Sidebar: The Single User Parameter & Engine Controls
# -----------------------------------------------------------------------------
st.sidebar.title("⚡ Autonomous Harvester")

if not client.api_key:
    st.sidebar.error("⚠️ POLYSIMULATOR_API_KEY is not configured in .env!")
    st.stop()

st.sidebar.markdown(
    """
    **Autonomous Operation:**  
    The bot on its own scans candidate markets, evaluates net marginal yields, sizes quote shares ($Q^*$),
    and executes complementary binary quotes (`BUY YES` + `BUY NO`).
    """
)
st.sidebar.markdown("---")

# The ONLY user parameter: Unlocked Capital Fraction
st.sidebar.subheader("💰 Capital Allocation")
unlocked_capital = st.sidebar.slider(
    "Unlocked Trading Capital ($)",
    min_value=25.0,
    max_value=500.0,
    value=100.0,
    step=25.0,
    help="Fraction of your account balance unlocked for the bot. We set this to $100 to simulate real-money deployment.",
)

# Get or initialize the Autonomous Daemon
daemon = AutonomousHarvesterDaemon.get_instance(unlocked_capital_usd=unlocked_capital)
daemon.update_capital(unlocked_capital)

st.sidebar.markdown("---")
st.sidebar.subheader("🎮 Engine Lifecycle")

col_btn1, col_btn2 = st.sidebar.columns(2)
if not daemon.is_running():
    if col_btn1.button("▶️ Start Engine", type="primary", use_container_width=True):
        daemon.start()
        st.sidebar.success("Engine started!")
        time.sleep(1)
        st.rerun()
else:
    if col_btn1.button("⏸️ Pause Engine", use_container_width=True):
        daemon.stop()
        st.sidebar.warning("Engine paused.")
        time.sleep(1)
        st.rerun()

if col_btn2.button("🛑 Kill-Switch", use_container_width=True):
    daemon.stop()
    client.cancel_all()
    for o in db.get_live_orders():
        db.update_order_status(o["order_id"], "cancelled")
    st.sidebar.error("Emergency kill-switch executed.")
    time.sleep(1)
    st.rerun()

# Auto-refresh
st.sidebar.markdown("---")
auto_refresh = st.sidebar.checkbox("Auto-refresh Telemetry", value=True)
refresh_interval = st.sidebar.slider("Refresh Cadence (s)", min_value=2, max_value=20, value=4)

# -----------------------------------------------------------------------------
# Main Header: Executive Status & Telemetry
# -----------------------------------------------------------------------------
is_active = daemon.is_running()

col_title, col_status = st.columns([4, 1])
with col_title:
    st.title("⚡ Autonomous Liquidity Rewards Terminal")
    st.caption("Real-Time Telemetry & Performance Analytics for Cloud Paper Trading on PolySimulator")

with col_status:
    if is_active:
        st.markdown('<div class="status-running">● AUTONOMOUS BOT ACTIVE</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="status-paused">■ ENGINE PAUSED</div>', unsafe_allow_html=True)

try:
    bal = client.get_balance()
except Exception:
    bal = None

# Top Metrics Row
m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    st.metric("Total Equity", f"${bal.total_value:.2f}" if bal else "$100.00")
with m2:
    st.metric("Unlocked Capital Cap", f"${unlocked_capital:.2f}", help="Total capital cap allocated to the autonomous bot")
with m3:
    active_mkt_title = daemon.current_market.get("question", "Scanning...") if daemon.current_market else "Scanning Universe..."
    st.metric("Autonomous Market", active_mkt_title[:24] + "..." if len(active_mkt_title) > 24 else active_mkt_title)
with m4:
    st.metric("Quote Size (Q*)", f"{daemon.current_quote_size:.0f} shares", help="Autonomously sized by the capital optimizer")
with m5:
    cum_rewards = db.get_total_cumulative_rewards()
    st.metric("Accumulated Rewards", f"${cum_rewards:.2f}", delta="Cleared $1 Floor" if cum_rewards >= 1.0 else "Accruing")

st.markdown("---")

# -----------------------------------------------------------------------------
# Main Tabs: 1. Autonomous Brain | 2. Live Microstructure | 3. Rewards Analytics | 4. Universe Leaderboard
# -----------------------------------------------------------------------------
tab_brain, tab_book, tab_analytics, tab_leaderboard, tab_audit = st.tabs([
    "🧠 Autonomous Decision Reasoner",
    "📊 Live Book & Active Quotes",
    "📈 Rewards & Performance Analytics",
    "🏆 Opportunity Universe Leaderboard",
    "📑 Audit Log & Positions",
])

# =============================================================================
# TAB 1: Autonomous Decision Reasoner (The "Brain" Panel)
# =============================================================================
with tab_brain:
    st.subheader("🧠 Algorithmic Decision & Optimization Rationale")
    st.markdown(
        """
        The bot makes all capital allocation, sizing, and market selection decisions mathematically.
        Below is the transparent reasoning output generated by the optimizer.
        """
    )

    latest_decision = db.get_latest_decision()
    if latest_decision:
        dec_time = datetime.fromtimestamp(latest_decision["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        st.markdown(
            f"""
            <div class="decision-box">
                <b>[TIMESTAMP: {dec_time}] {latest_decision['decision_type']}</b><br/>
                <b>Target:</b> {latest_decision['market_question']}<br/>
                <b>Allocated Quoting Capital:</b> ${latest_decision['allocated_capital']:.2f} | <b>Quote Size Q*:</b> {latest_decision['quote_size']:.0f} shares<br/>
                <b>Estimated Net Yield:</b> +${latest_decision['net_yield_est']:.2f}/day<br/><br/>
                <b>Optimizer Rationale:</b><br/>
                {latest_decision['rationale']}
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.info("The autonomous bot is initializing its first scan. Rationale will appear immediately.")

    st.markdown("#### Recent Decision History")
    recent_decisions = db.get_recent_decisions(limit=8)
    if recent_decisions:
        df_dec = pd.DataFrame(recent_decisions)
        df_dec["timestamp"] = pd.to_datetime(df_dec["timestamp"], unit="s")
        st.dataframe(
            df_dec[["timestamp", "decision_type", "market_question", "allocated_capital", "quote_size", "net_yield_est", "rationale"]],
            hide_index=True,
            use_container_width=True,
        )

# =============================================================================
# TAB 2: Live Book & Active Quotes
# =============================================================================
with tab_book:
    st.subheader("📊 Live Order Book & Active Resting Quotes")

    if daemon.current_market and daemon.current_yes_token:
        st.markdown(f"**Target Market:** `{daemon.current_market.get('question')}`")
        st.caption(f"Condition: `{daemon.current_market.get('condition_id')}` | YES Token: `{daemon.current_yes_token[:20]}...`")

        col_depth, col_my_quotes = st.columns([3, 2])

        with col_depth:
            st.markdown("#### Live PolySimulator L2 Order Book")
            try:
                book = client.get_book(daemon.current_yes_token, depth=12)
                raw_bids = book.get("bids", [])
                raw_asks = book.get("asks", [])

                fmt_bids = [
                    {"Side": "BID", "Price": f"${float(b['price']):.3f}", "Size": f"{float(b['size']):.1f}", "Value": f"${float(b['price'])*float(b['size']):.2f}"}
                    for b in reversed(raw_bids)
                ]
                fmt_asks = [
                    {"Side": "ASK", "Price": f"${float(a['price']):.3f}", "Size": f"{float(a['size']):.1f}", "Value": f"${float(a['price'])*float(a['size']):.2f}"}
                    for a in reversed(raw_asks)
                ]

                cb, ca = st.columns(2)
                with cb:
                    st.markdown("**Bids (YES)**")
                    if fmt_bids:
                        st.dataframe(pd.DataFrame(fmt_bids), hide_index=True, use_container_width=True)
                with ca:
                    st.markdown("**Asks (YES)**")
                    if fmt_asks:
                        st.dataframe(pd.DataFrame(fmt_asks), hide_index=True, use_container_width=True)
            except Exception as e:
                st.error(f"Error fetching order book: {e}")

        with col_my_quotes:
            st.markdown("#### Bot's Resting Maker Quotes on PolySimulator")
            try:
                open_orders = client.get_open_orders()
                if open_orders:
                    view_orders = []
                    for o in open_orders:
                        view_orders.append({
                            "Order ID": str(o.get("order_id") or o.get("id")),
                            "Outcome": o.get("outcome", ""),
                            "Side": o.get("side", ""),
                            "Price": f"${float(o.get('limit_price') or o.get('price') or 0):.3f}",
                            "Size": f"{float(o.get('quantity') or o.get('size') or 0):.0f}",
                            "Status": o.get("status", "PENDING"),
                        })
                    st.dataframe(pd.DataFrame(view_orders), hide_index=True, use_container_width=True)
                else:
                    st.info("No active resting quotes currently. Bot will place them during quoting loop.")
            except Exception as e:
                st.error(f"Error reading open orders: {e}")

            # Collateral & Capital Utilization Gauge
            quoting_capital = daemon.current_quote_size * (daemon.current_mid or 0.50) * 2
            free_cash = max(0.0, unlocked_capital - quoting_capital)
            st.markdown("#### Capital Utilization ($100 Cap)")
            st.progress(min(1.0, quoting_capital / unlocked_capital))
            st.write(f"**Quoting Collateral Held:** `${quoting_capital:.2f}` | **Liquid Reserve Buffer:** `${free_cash:.2f}`")
    else:
        st.info("Engine is currently evaluating universe. Book and active quotes will display momentarily.")

# =============================================================================
# TAB 3: Rewards & Performance Analytics
# =============================================================================
with tab_analytics:
    st.subheader("📈 Liquidity Rewards & Quantitative Analytics")

    daily_summary = db.get_daily_summary()
    accrued_today = daily_summary.get("accumulated_reward_usd", 0.0) if daily_summary else 0.0
    payout_today = daily_summary.get("payout_usd", 0.0) if daily_summary else 0.0
    cleared = bool(daily_summary.get("cleared_floor", 0)) if daily_summary else False
    epochs_sampled = daily_summary.get("total_epochs", 0) if daily_summary else 0

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Accrued Rewards Today", f"${accrued_today:.4f}")
    with c2:
        st.metric("Daily Payout ($1.00 Floor)", f"${payout_today:.2f}", delta="Floor Cleared!" if cleared else "Floor Pending")
    with c3:
        st.metric("Daily Sampling Epochs", f"{epochs_sampled} / 1,440")
    with c4:
        user_sc = compute_quadratic_score(daemon.max_spread_cents, daemon.spread_offset_cents, daemon.current_quote_size * 2)
        st.metric("Quote Quadratic Score", f"{user_sc:.1f} pts")

    # Milestone Progress Bar
    floor_pct = min(1.0, accrued_today / 1.0)
    st.write(f"**Daily Floor Clearance Progress:** `${accrued_today:.3f}` / `$1.000` ({floor_pct*100:.1f}%)")
    st.progress(floor_pct)

    # Historical Graphs from SQLite
    with db._get_connection() as conn:
        df_epochs = pd.read_sql_query(
            "SELECT timestamp, epoch_reward_usd, user_score, competing_score FROM reward_epochs ORDER BY timestamp DESC LIMIT 120",
            conn,
        )

    if not df_epochs.empty:
        df_epochs["time"] = pd.to_datetime(df_epochs["timestamp"], unit="s")
        df_epochs["cum_rewards"] = df_epochs["epoch_reward_usd"][::-1].cumsum()[::-1]

        g1, g2 = st.columns(2)
        with g1:
            st.markdown("#### Cumulative Accrued Rewards ($)")
            st.line_chart(df_epochs.set_index("time")[["cum_rewards"]], height=260)
        with g2:
            st.markdown("#### User Score vs Competing Market Maker Depth")
            st.line_chart(df_epochs.set_index("time")[["user_score", "competing_score"]], height=260)
    else:
        st.info("Rewards epoch data is accumulating. Charts update every 60 seconds.")

# =============================================================================
# TAB 4: Opportunity Universe Leaderboard
# =============================================================================
with tab_leaderboard:
    st.subheader("🏆 Dynamic Market Opportunity Leaderboard")
    st.markdown(
        """
        The bot scans Polymarket and PolySimulator head pools, scoring each market by **Net Marginal Yield per Dollar**
        after taking into account quadratic reward shares, spread capture, category adverse selection, and gas fees.
        """
    )
    try:
        raw_mkts = client.get_markets(limit=20, hot_only=True)
        if not raw_mkts:
            raw_mkts = client.get_markets(limit=20)

        leaderboard_data = []
        for m in raw_mkts:
            if m.get("closed") or len(m.get("clob_token_ids", [])) < 2:
                continue
            pool = float(m.get("reward_pool", 2000.0))
            tok = m["clob_token_ids"][0]
            mid = client.get_midpoint(tok) or 0.50
            # Sizing based on unlocked capital
            q = min(100.0, max(50.0, unlocked_capital * 0.75))
            u_score = compute_quadratic_score(3.0, 0.75, q * 2)
            c_score = 15000.0  # approximate baseline
            est_payout = (u_score / (u_score + c_score)) * pool
            net_yield_dollar = est_payout / max(1.0, unlocked_capital * 0.75)

            is_chosen = bool(daemon.current_market and daemon.current_market.get("condition_id") == m.get("condition_id"))
            leaderboard_data.append({
                "Status": "⭐ CURRENT" if is_chosen else "Candidate",
                "Market Question": m.get("question", "")[:45] + "...",
                "Daily Pool ($)": f"${pool:,.0f}",
                "Mid Price": f"${mid:.3f}",
                "Sized Q*": f"{q:.0f}",
                "Est. Daily Reward": f"${est_payout:.2f}",
                "Net Yield / $": f"${net_yield_dollar:.4f}",
                "Floor >= $1": "✅" if est_payout >= 1.0 else "❌",
            })

        if leaderboard_data:
            st.dataframe(pd.DataFrame(leaderboard_data), hide_index=True, use_container_width=True)
    except Exception as e:
        st.error(f"Error generating leaderboard: {e}")

# =============================================================================
# TAB 5: Audit Log & Positions
# =============================================================================
with tab_audit:
    st.subheader("📑 Execution Audit Log & Positions")

    col_p, col_o = st.columns(2)
    with col_p:
        st.markdown("#### Open Token Positions (PolySimulator)")
        try:
            pos = client.get_positions(status="open")
            if pos:
                st.dataframe(pd.DataFrame(pos), hide_index=True, use_container_width=True)
            else:
                st.info("No token positions held.")
        except Exception as e:
            st.error(f"Error reading positions: {e}")

    with col_o:
        st.markdown("#### Recent Database Orders")
        with db._get_connection() as conn:
            df_ord = pd.read_sql_query(
                "SELECT order_id, outcome, side, price, size, status, placed_at FROM orders ORDER BY placed_at DESC LIMIT 30",
                conn,
            )
        if not df_ord.empty:
            df_ord["placed_at"] = pd.to_datetime(df_ord["placed_at"], unit="s")
            st.dataframe(df_ord, hide_index=True, use_container_width=True)

# -----------------------------------------------------------------------------
# Auto-refresh Loop
# -----------------------------------------------------------------------------
if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
