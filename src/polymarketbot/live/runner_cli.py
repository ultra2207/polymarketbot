"""Command-line surface and the live-trading confirmation gate.

``parse_args`` defines the operator flags; ``require_live_confirmation`` is the
only gate that lets a run place real orders. ``polymarketbot.live.runner``
re-exports both and remains the module entry point.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from polymarketbot.logs import get_logger

log = get_logger("real_money_trader")

# The exact phrase an operator must type when ``--live`` is given without ``--yes``.
LIVE_CONFIRMATION_PHRASE = "START LIVE"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Polymarket real-money reward market-making runner. Dry-run unless "
            "--live is supplied; --live additionally requires a ready preflight."
        )
    )
    parser.add_argument(
        "--condition-id",
        type=str,
        default=None,
        help="Polymarket condition ID to trade (default: auto-select a rewards market).",
    )
    parser.add_argument(
        "--token-yes",
        type=str,
        default=None,
        help="CLOB token id for the YES outcome (required when the condition id is not in the rewards list).",
    )
    parser.add_argument(
        "--token-no",
        type=str,
        default=None,
        help="CLOB token id for the NO outcome (required when the condition id is not in the rewards list).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Place REAL orders. Without this flag the runner is a dry-run even with credentials.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive START LIVE confirmation (only meaningful with --live).",
    )
    parser.add_argument(
        "--record-live-data",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Write the private session (orders, fills, quotes, account, risk, rewards) "
            "to data/live_tape. Default: on for live, off for dry-run."
        ),
    )
    parser.add_argument(
        "--budget-pct",
        type=float,
        default=1.0,
        help="Fraction of the CLOB wallet used as a sizing cap (0.20 or 20 for 20%%, default: 1.0).",
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=None,
        help="Absolute sizing-cap ceiling in USD (default: none).",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=100.0,
        help="Dry-run only: simulated wallet balance in USD (default: 100.0). Ignored when live.",
    )
    parser.add_argument(
        "--quote-size",
        type=float,
        default=None,
        help="Maker quote size in shares (default: derived from the sizing cap).",
    )
    parser.add_argument(
        "--spread-offset",
        type=float,
        default=None,
        help="Half-spread offset from mid in cents (default: optimized for the market).",
    )
    parser.add_argument(
        "--daily-reward",
        type=float,
        default=None,
        help="Daily reward pool in USD (default: from the market rewards config).",
    )
    parser.add_argument(
        "--max-spread",
        type=float,
        default=None,
        help="Qualifying max spread in cents for rewards (default: from the market).",
    )
    parser.add_argument(
        "--min-size",
        type=float,
        default=None,
        help="Qualifying minimum order size in shares (default: from the market).",
    )
    parser.add_argument(
        "--competing-score",
        type=float,
        default=None,
        help="Hardcoded competing L2 score override (default: audited from live CLOB L2 depth).",
    )
    parser.add_argument(
        "--auto-merge-threshold",
        type=float,
        default=10.0,
        help="Complete-set threshold before the engine attempts a CTF merge (default: 10.0).",
    )
    parser.add_argument(
        "--legacy-mode",
        action="store_true",
        help="Quote BUY YES + SELL YES instead of dual-cash BUY YES + BUY NO.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duration to run in seconds (default: until Ctrl+C).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Dashboard render interval in seconds (default: 1.0s).",
    )
    parser.add_argument(
        "--reward-refresh-interval",
        type=float,
        default=60.0,
        help="L2 competing-score and reward-epoch refresh interval in seconds (default: 60.0s).",
    )
    parser.add_argument(
        "--refit-every",
        type=float,
        default=3600.0,
        help=(
            "Seconds between bounded tape refits in the background (default: 3600s). "
            "The first block runs at startup; 0 disables refitting."
        ),
    )
    parser.add_argument(
        "--refit-max-partitions",
        type=int,
        default=1,
        help="Partitions one background refit block may process (default: 1).",
    )
    parser.add_argument(
        "--refit-partition-timeout",
        type=float,
        default=60.0,
        help="Seconds one fitter subprocess may run inside a block (default: 60s).",
    )
    parser.add_argument(
        "--max-feed-staleness",
        type=float,
        default=30.0,
        help="Seconds without a feed frame before quoting is halted (default: 30.0s).",
    )
    parser.add_argument(
        "--redeem-on-close",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Redeem resolved positions after the market closes (default: enabled).",
    )
    parser.add_argument(
        "--allow-no-settlement",
        action="store_true",
        help=(
            "Allow live quoting when on-chain CTF settlement is unavailable (proxy wallet "
            "or no signing key). Inventory can then only be exited by selling on the CLOB."
        ),
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="data/real_money_trading.db",
        help="SQLite database path for state persistence.",
    )
    parser.add_argument(
        "--ledger-path",
        type=str,
        default="data/real_money_trading_ledger.json",
        help="JSON ledger path for the audit trail.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Disable ANSI screen clearing for log-friendly streaming.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run even if --live is also supplied.",
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        default=None,
        help="Polygon JSON-RPC endpoint URL override.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------
def require_live_confirmation(
    live: bool,
    yes: bool,
    stdin_is_tty: bool,
    ask: Callable[[str], str] = input,
) -> bool:
    """Return True only when real orders are allowed to be placed.

    Dry-run runs never need confirmation. ``--yes`` acknowledges ``--live``
    non-interactively. Otherwise the operator must have a TTY and type the exact
    phrase ``START LIVE``.
    """
    if not live:
        return True
    if yes:
        return True
    if not stdin_is_tty:
        log.error(
            "Refusing to trade live: stdin is not a terminal, so the START LIVE "
            "confirmation cannot be requested. Re-run with --yes only if you "
            "intend to place real orders."
        )
        return False
    try:
        answer = ask(f"Type '{LIVE_CONFIRMATION_PHRASE}' to place REAL orders on Polymarket: ")
    except (EOFError, KeyboardInterrupt):
        log.error("Live confirmation aborted: no answer was read from stdin.")
        return False
    return answer.strip() == LIVE_CONFIRMATION_PHRASE
