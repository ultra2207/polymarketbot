"""Executive telemetry for the live paper soak.

Run with ``streamlit run src/polymarketbot/ui/app.py``. Every panel reads the
SQLite journal written by ``python -m polymarketbot.paper.main``
(``data/paper_engine.db``: ``trades``, ``snapshots``, ``lots``). The dashboard
never imports the trading engine and never starts or stops it: the soak is its
own process, so the UI is read-only by construction.

The UI is imported in tests without a Streamlit runtime; ``streamlit`` and
``pandas`` are imported inside :func:`main`, so a plain ``import
polymarketbot.ui.app`` stays cheap and side-effect free.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from polymarketbot.logs import IST

DB_PATH = Path(os.environ.get("POLYMARKETBOT_PAPER_DB", "data/paper_engine.db"))

SNAPSHOT_COLUMNS = ("ts", "strategy", "cash", "positions_value", "equity")
TRADE_COLUMNS = (
    "ts",
    "strategy",
    "kind",
    "condition_id",
    "token_id",
    "shares",
    "price",
    "fee",
    "cash_delta",
    "detail",
)
LOT_COLUMNS = (
    "strategy",
    "condition_id",
    "token_id",
    "shares",
    "cost_usd",
    "par_value",
    "last_mark",
    "tag",
)


def read_table(db_path: Path | str, table: str, columns: tuple[str, ...],
               order_by: str = "", limit: int | None = None) -> list[dict[str, Any]]:
    """Read one journal table, returning ``[]`` when the soak has not run yet."""
    path = Path(db_path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    query = f"SELECT {', '.join(columns)} FROM {table}"
    if order_by:
        query += f" ORDER BY {order_by}"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def read_snapshots(db_path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    return read_table(db_path, "snapshots", SNAPSHOT_COLUMNS, order_by="ts")


def read_trades(db_path: Path | str = DB_PATH, limit: int = 500) -> list[dict[str, Any]]:
    return read_table(db_path, "trades", TRADE_COLUMNS, order_by="ts DESC", limit=limit)


def read_lots(db_path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    return read_table(db_path, "lots", LOT_COLUMNS, order_by="strategy, condition_id")


def _first_and_last(snapshots: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, dict]]:
    first: dict[str, dict[str, Any]] = {}
    last: dict[str, dict[str, Any]] = {}
    for row in snapshots:
        strategy = row["strategy"]
        if strategy not in first:
            first[strategy] = row
        last[strategy] = row
    return first, last


def summarise(snapshots: list[dict[str, Any]], trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the journal into the headline numbers the panels show."""
    first, last = _first_and_last(snapshots)
    strategies = sorted(last)
    total_equity = sum(float(last[name]["equity"]) for name in strategies)
    initial_equity = sum(float(first[name]["equity"]) for name in strategies)
    cash = sum(float(last[name]["cash"]) for name in strategies)
    positions_value = sum(float(last[name]["positions_value"]) for name in strategies)
    rows = []
    for name in strategies:
        trades_for = [t for t in trades if t["strategy"] == name]
        rows.append({
            "Strategy": name,
            "Equity": float(last[name]["equity"]),
            "Cash": float(last[name]["cash"]),
            "Positions": float(last[name]["positions_value"]),
            "PnL": float(last[name]["equity"]) - float(first[name]["equity"]),
            "Trades": len(trades_for),
            "Fees": sum(float(t["fee"]) for t in trades_for),
            "Last snapshot": last[name]["ts"],
        })
    return {
        "strategies": strategies,
        "total_equity": total_equity,
        "initial_equity": initial_equity,
        "pnl": total_equity - initial_equity,
        "cash": cash,
        "positions_value": positions_value,
        "trade_count": len(trades),
        "fees": sum(float(t["fee"]) for t in trades),
        "last_snapshot": max((float(last[name]["ts"]) for name in strategies), default=None),
        "rows": rows,
    }


def lot_value(lot: dict[str, Any]) -> float:
    """Value one lot the way the ledger does: par when known, else the mark."""
    shares = float(lot["shares"])
    if lot["par_value"] is not None:
        return shares * float(lot["par_value"])
    return shares * float(lot["last_mark"])


def format_ts(ts: float | None) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(float(ts), tz=IST).strftime("%Y-%m-%d %H:%M:%S IST")


def main() -> None:
    import time

    import pandas as pd
    import streamlit as st

    db_path = Path(os.environ.get("POLYMARKETBOT_PAPER_DB", str(DB_PATH)))

    st.set_page_config(
        page_title="Paper Soak Telemetry",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        """
        <style>
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
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.sidebar.title("⚡ Paper Soak")
    st.sidebar.caption(f"Journal: `{db_path}`")
    st.sidebar.markdown(
        "Read-only telemetry. Start the soak in its own terminal:\n\n"
        "`python -m polymarketbot.paper.main`"
    )
    auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
    refresh_interval = st.sidebar.slider("Refresh cadence (s)", 2, 20, 5)
    if st.sidebar.button("🔄 Refresh now", use_container_width=True):
        st.rerun()

    snapshots = read_snapshots(db_path)
    trades = read_trades(db_path)
    lots = read_lots(db_path)
    state = summarise(snapshots, trades)

    col_title, col_status = st.columns([4, 1])
    with col_title:
        st.title("⚡ Live Paper Trading Telemetry")
        st.caption(
            "RewardHarvester · SettlementHarvester · CtfMinter — journal-backed, "
            "no simulated prices in this view."
        )
    with col_status:
        if state["last_snapshot"] is None:
            st.markdown('<div class="status-paused">■ NO SNAPSHOTS</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="status-running">● JOURNAL LIVE</div>', unsafe_allow_html=True)
            st.caption(f"Last snapshot {format_ts(state['last_snapshot'])}")

    if not snapshots and not trades:
        st.info(
            "The journal is empty. Run `python -m polymarketbot.paper.main` (add `--fresh` "
            "to ignore the last durable snapshot) and this page will fill in."
        )
        st.stop()

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Equity", f"${state['total_equity']:.2f}")
    m2.metric(
        "Cumulative PnL",
        f"${state['pnl']:+.2f}",
        delta=f"{state['pnl'] / state['initial_equity'] * 100.0:+.2f} %"
        if state["initial_equity"] else None,
    )
    m3.metric("Cash", f"${state['cash']:.2f}")
    m4.metric("Positions Value", f"${state['positions_value']:.2f}")
    m5.metric("Trades", f"{state['trade_count']}", help=f"Fees paid: ${state['fees']:.2f}")

    st.markdown("---")
    tab_perf, tab_positions, tab_trades, tab_strategies = st.tabs([
        "📈 Performance",
        "📦 Positions & Lots",
        "📑 Trade Journal",
        "🧩 Strategies",
    ])

    with tab_perf:
        st.subheader("Equity and Cash Over Time")
        if snapshots:
            frame = pd.DataFrame(snapshots)
            frame["time"] = pd.to_datetime(frame["ts"], unit="s")
            equity = frame.pivot_table(
                index="time", columns="strategy", values="equity", aggfunc="last"
            ).sort_index().ffill()
            equity["total"] = equity.sum(axis=1)
            st.markdown("#### Equity per strategy")
            st.line_chart(equity, height=320)
            cash = frame.pivot_table(
                index="time", columns="strategy", values="cash", aggfunc="last"
            ).sort_index().ffill()
            st.markdown("#### Cash per strategy")
            st.line_chart(cash, height=260)
        else:
            st.info("No snapshots recorded yet.")

    with tab_positions:
        st.subheader("Open Lots")
        if lots:
            frame = pd.DataFrame(lots)
            frame["value"] = [lot_value(lot) for lot in lots]
            frame["cost_usd"] = frame["cost_usd"].astype(float)
            frame["unrealised"] = frame["value"] - frame["cost_usd"]
            st.dataframe(
                frame[[
                    "strategy", "condition_id", "token_id", "shares", "cost_usd",
                    "par_value", "last_mark", "value", "unrealised", "tag",
                ]].rename(columns={
                    "strategy": "Strategy",
                    "condition_id": "Condition",
                    "token_id": "Token",
                    "shares": "Shares",
                    "cost_usd": "Cost",
                    "par_value": "Par",
                    "last_mark": "Mark",
                    "value": "Value",
                    "unrealised": "Unrealised",
                    "tag": "Tag",
                }),
                hide_index=True,
                use_container_width=True,
            )
            st.caption(f"Total lot value: ${sum(lot_value(lot) for lot in lots):.2f}")
        else:
            st.info("No inventory held: every quote is resting or has been settled.")

    with tab_trades:
        st.subheader("Journal")
        if trades:
            strategies = sorted({t["strategy"] for t in trades})
            kinds = sorted({t["kind"] for t in trades})
            picked_strategies = st.multiselect("Strategy", strategies, default=strategies)
            picked_kinds = st.multiselect("Kind", kinds, default=kinds)
            shown = [
                t for t in trades
                if t["strategy"] in picked_strategies and t["kind"] in picked_kinds
            ]
            frame = pd.DataFrame(shown)
            if not frame.empty:
                frame["time"] = pd.to_datetime(frame["ts"], unit="s")
                st.dataframe(
                    frame[[
                        "time", "strategy", "kind", "condition_id", "token_id",
                        "shares", "price", "fee", "cash_delta", "detail",
                    ]].rename(columns={
                        "time": "Time",
                        "strategy": "Strategy",
                        "kind": "Kind",
                        "condition_id": "Condition",
                        "token_id": "Token",
                        "shares": "Shares",
                        "price": "Price",
                        "fee": "Fee",
                        "cash_delta": "Cash Δ",
                        "detail": "Detail",
                    }),
                    hide_index=True,
                    use_container_width=True,
                )
            else:
                st.info("No trades match the selected filters.")
        else:
            st.info("No trades recorded yet.")

    with tab_strategies:
        st.subheader("Per-Strategy Accounting")
        if state["rows"]:
            table = pd.DataFrame(state["rows"])
            table["Last snapshot"] = [format_ts(row["Last snapshot"]) for row in state["rows"]]
            st.dataframe(table, hide_index=True, use_container_width=True)
        else:
            st.info("No strategies have written a snapshot yet.")
        if trades:
            st.markdown("#### Trades by kind")
            frame = pd.DataFrame(trades)
            counts = frame.pivot_table(
                index="strategy", columns="kind", values="ts", aggfunc="count", fill_value=0
            )
            st.dataframe(counts, use_container_width=True)

    if auto_refresh:
        time.sleep(refresh_interval)
        st.rerun()


if __name__ == "__main__":
    main()
