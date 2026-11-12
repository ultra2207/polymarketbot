"""Terminal dashboard, banner and shutdown summary for the live runner.

Pure rendering: every function reads a telemetry dict and writes stdout, so a
renderer bug can never touch engine state.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Any

from polymarketbot.logs import IST

# ANSI color codes for high-density terminal telemetry
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_WHITE = "\033[37m"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def _fmt_value(value: Any, spec: str = ".2f", na: str = "n/a") -> str:
    """Format a numeric value, or return ``na`` for anything non-numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return na
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return na


def _num(telemetry: dict[str, Any], key: str, spec: str = ".2f", na: str = "n/a") -> str:
    return _fmt_value(telemetry.get(key), spec, na)


def _text(telemetry: dict[str, Any], key: str, na: str = "n/a") -> str:
    value = telemetry.get(key)
    if value is None or value == "":
        return na
    return str(value)


def build_dashboard_lines(
    telemetry: dict[str, Any],
    market_info: dict[str, Any],
    elapsed: float,
    now_str: str,
) -> list[str]:
    """Render telemetry into terminal lines. Missing keys render as ``n/a``."""
    t = telemetry or {}
    question = str(market_info.get("question", "n/a"))
    token_yes = str(market_info.get("token_yes", "n/a"))
    token_no = str(market_info.get("token_no", "n/a"))

    mode = _text(t, "mode")
    if mode == "REAL_MONEY":
        mode_str = f"{C_GREEN}[LIVE REAL-MONEY TRADING]{C_RESET}"
    elif mode == "DRY_RUN":
        mode_str = f"{C_YELLOW}[DRY-RUN / SAFETY INSPECTION]{C_RESET}"
    else:
        mode_str = f"{C_DIM}[MODE n/a]{C_RESET}"

    total_pnl = t.get("total_pnl")
    pnl_color = C_GREEN if isinstance(total_pnl, (int, float)) and total_pnl >= 0 else C_RED
    net_q = t.get("net_exposure")
    if isinstance(net_q, (int, float)):
        q_color = C_GREEN if abs(net_q) <= 15.0 else (C_YELLOW if abs(net_q) <= 50.0 else C_RED)
    else:
        q_color = C_DIM

    enabled = t.get("trading_enabled")
    if enabled is True:
        trading_str = f"{C_GREEN}ENABLED{C_RESET}"
    elif enabled is False:
        trading_str = f"{C_RED}HALTED{C_RESET}"
    else:
        trading_str = f"{C_DIM}n/a{C_RESET}"
    halt_reason = _text(t, "halt_reason", "-")

    book_stale = t.get("book_stale")
    if book_stale is True:
        book_str = f"{C_RED}STALE{C_RESET}"
    elif book_stale is False:
        book_str = f"{C_GREEN}FRESH{C_RESET}"
    else:
        book_str = f"{C_DIM}n/a{C_RESET}"
    feed_age = _num(market_info, "feed_age_seconds", ".1f")

    equity = t.get("equity")
    initial_equity = t.get("initial_equity")
    if isinstance(equity, (int, float)) and isinstance(initial_equity, (int, float)):
        equity_delta = f"{equity - initial_equity:+.2f}"
        equity_color = C_GREEN if equity >= initial_equity else C_RED
    else:
        equity_delta = "n/a"
        equity_color = C_DIM
    equity_value = _fmt_value(equity, ".2f")

    deskew = t.get("latest_deskew") or {}
    if not isinstance(deskew, dict):
        deskew = {}
    pathway = str(deskew.get("pathway", "HOLD_STABLE"))
    urgency = str(deskew.get("urgency_level", "LOW"))
    urgency_color = C_GREEN if urgency == "LOW" else (C_YELLOW if urgency == "MEDIUM" else C_RED)
    savings = _num(deskew, "synthetic_savings_cents", ".2f")

    modelled_rewards = t.get("modelled_rewards", t.get("total_rewards"))
    modelled_value = _fmt_value(modelled_rewards, ".4f")
    run_rate = t.get("projected_daily_usd")
    if isinstance(run_rate, (int, float)) and run_rate > 0.0:
        proj_daily_str = f"${float(run_rate):,.2f} (run-rate)"
    else:
        proj_daily_str = "n/a"
    accrued_daily = t.get("accrued_daily_usd")
    floor_str = (
        f"${float(accrued_daily):,.4f} / $1.00"
        if isinstance(accrued_daily, (int, float))
        else "n/a"
    )

    user_score = 0.0
    quotes = t.get("active_quotes")
    if not isinstance(quotes, list):
        quotes = []
    for quote in quotes:
        if isinstance(quote, dict):
            score = quote.get("score")
            if isinstance(score, (int, float)):
                user_score += score

    lines: list[str] = []
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}{C_WHITE}[MM] POLYMARKET REAL-MONEY TRADING ENGINE | CLOB & ON-CHAIN CTF {mode_str}{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}Market:{C_RESET} {question[:76]}")
    lines.append(
        f"{C_DIM}Condition:{C_RESET} {str(t.get('condition_id') or market_info.get('condition_id') or 'n/a')[:18]}... | "
        f"{C_DIM}Time:{C_RESET} {now_str} | "
        f"{C_DIM}Runtime:{C_RESET} {elapsed:.1f}s | "
        f"{C_DIM}Order version:{C_RESET} {_num(t, 'order_version', 'd')} | "
        f"{C_BOLD}Size:{C_RESET} {_num(market_info, 'quote_size', '.1f')}sh | "
        f"{C_BOLD}Offset:{C_RESET} {_num(market_info, 'spread_offset_cents', '.2f')}c"
    )
    lines.append(f"{C_DIM}Tokens:{C_RESET} YES: {token_yes[:18]}... | NO: {token_no[:18]}...")
    lines.append(f"{C_BOLD}Trading:{C_RESET} {trading_str} | {C_BOLD}Halt reason:{C_RESET} {halt_reason[:40]} | "
                 f"{C_BOLD}Market closed:{C_RESET} {_text(t, 'market_closed')} | "
                 f"{C_BOLD}Book:{C_RESET} {book_str} | "
                 f"{C_BOLD}Feed age:{C_RESET} {feed_age}s | "
                 f"{C_BOLD}Breakers:{C_RESET} {_text(t, 'breaker_status')}")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[WALLET & BALANCES] CLOB COLLATERAL (NOT ON-CHAIN USDC){C_RESET}")
    lines.append(
        f"  {C_BOLD}Total Wallet:{C_RESET}   ${_num(t, 'total_wallet_balance', '8.2f')}    "
        f"{C_BOLD}Free Cash:{C_RESET}      ${_num(t, 'free_cash', '8.2f')}    "
        f"{C_BOLD}Locked Cash:{C_RESET}    ${_num(t, 'locked_cash', '8.2f')}"
    )
    lines.append(
        f"  {C_BOLD}Equity:{C_RESET}         {equity_color}${equity_value:>8s}{C_RESET} "
        f"({equity_delta} vs start ${_num(t, 'initial_equity', '.2f')})    "
        f"{C_BOLD}Sizing Cap:{C_RESET} ${_num(t, 'trading_bankroll', '.2f')} "
        f"({_num(t, 'budget_pct', '.1f')}% of wallet, cap not cash)"
    )
    lines.append(
        f"  {C_BOLD}POL Gas:{C_RESET}        {_num(t, 'pol_balance', '.4f')} POL    "
        f"{C_BOLD}Fees Paid:{C_RESET}      ${_num(t, 'fees_paid', '.4f')}    "
        f"{C_BOLD}Gas (CTF):{C_RESET}      ${_num(t, 'total_gas_fees', '.3f')}"
    )
    lines.append(
        f"  {C_BOLD}Shares YES:{C_RESET}     {_num(t, 'shares_yes', '8.2f')}    "
        f"{C_BOLD}Shares NO:{C_RESET}       {_num(t, 'shares_no', '8.2f')}    "
        f"{C_BOLD}Complete Sets:{C_RESET}  {_num(t, 'complete_sets', '8.2f')}    "
        f"{C_BOLD}Net Exposure:{C_RESET}   {q_color}{_num(t, 'net_exposure', '+8.2f')}{C_RESET}"
    )
    lines.append(
        f"  {C_BOLD}TOTAL PnL:{C_RESET}      {pnl_color}${_num(t, 'total_pnl', '+8.4f')}{C_RESET}    "
        f"{C_BOLD}Realized:{C_RESET}       ${_num(t, 'realized_pnl', '.4f')}    "
        f"{C_BOLD}Cash Reserve:{C_RESET}   ${_num(t, 'reserve_cash_usd', '.2f')}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[DE-SKEW] 2-TIER DYNAMIC DE-SKEW OPTIMIZER{C_RESET}")
    lines.append(
        f"  {C_BOLD}Pathway:{C_RESET}        [{C_BOLD}{pathway:<16s}{C_RESET}]   "
        f"{C_BOLD}Urgency:{C_RESET} {urgency_color}{urgency:<10s}{C_RESET}   "
        f"{C_BOLD}Synthetic Savings:{C_RESET} {savings}c/sh"
    )
    if deskew.get("reason"):
        lines.append(f"  {C_DIM}Sentinel Action:{C_RESET} {str(deskew['reason'])[:74]}")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[REWARDS] POLYMARKET QUADRATIC REWARDS (1,440 Daily Epochs){C_RESET}")
    lines.append(
        f"  {C_BOLD}UTC Epoch:{C_RESET}  {_num(t, 'epoch_index', 'd')} / 1440    "
        f"{C_BOLD}User Score:{C_RESET}     {user_score:>8.2f}    "
        f"{C_BOLD}Daily Pool:{C_RESET}     ${_num(market_info, 'daily_reward', '7.2f')}"
    )
    actual_rewards_value = t.get("actual_rewards_usd", t.get("actual_rewards"))
    actual_rebates_value = t.get("actual_rebates_usd", t.get("actual_rebates"))
    actual_daily_value = t.get("actual_daily_rewards_usd", t.get("actual_daily_rewards"))
    actual_payout_value = t.get("actual_total_payout_usd", t.get("actual_total_payout"))
    lines.append(
        f"  {C_YELLOW}Modelled{C_RESET} rewards: ${modelled_value:>8s} (not settled)    "
        f"{C_GREEN}Actual{C_RESET} rewards:   ${_fmt_value(actual_rewards_value, '.4f')}    "
        f"{C_BOLD}Rebates:{C_RESET}        ${_fmt_value(actual_rebates_value, '.4f')}"
    )
    lines.append(
        f"  {C_BOLD}Actual Daily:{C_RESET}   ${_fmt_value(actual_daily_value, '.4f')}    "
        f"{C_BOLD}Actual Payout:{C_RESET}  ${_fmt_value(actual_payout_value, '.4f')}    "
        f"{C_BOLD}Proj. Daily:{C_RESET}    {proj_daily_str}    "
        f"{C_BOLD}Floor accrued:{C_RESET} {floor_str}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[ON-CHAIN CTF] POLYGON POS SETTLEMENT & COMPLETE-SET MERGES{C_RESET}")
    lines.append(
        f"  {C_BOLD}Total Merges:{C_RESET}   {_num(t, 'total_merges', 'd')}          "
        f"{C_BOLD}Total Splits:{C_RESET}   {_num(t, 'total_splits', 'd')}          "
        f"{C_BOLD}Total Fills:{C_RESET}    {_num(t, 'fills_count', 'd')}          "
        f"{C_BOLD}Traded Volume:{C_RESET}  ${_num(t, 'total_volume_usd', '.2f')}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[CLOB QUOTES] ACTIVE SIGNED ORDERS ON POLYMARKET CLOB{C_RESET}")
    lines.append(f"  {'Side':<6s} {'Outcome':<8s} {'Price':<8s} {'Size':<8s} {'Order ID':<20s} {'Score S(v,s)':<14s} {'Status'}")
    if quotes:
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            side = str(quote.get("side", "n/a"))
            side_color = C_GREEN if side == "BUY" else C_RED
            order_id = str(quote.get("order_id", "N/A"))[:18]
            lines.append(
                f"  {side_color}{side:<6s}{C_RESET} "
                f"{quote.get('token_side', 'n/a')!s:<8s} "
                f"${_num(quote, 'price', '.3f'):<7s} "
                f"{_num(quote, 'size', '.1f'):<8s} "
                f"{order_id:<20s} "
                f"{_num(quote, 'score', '.4f'):<14s} "
                f"{C_GREEN}{quote.get('status', 'LIVE')}{C_RESET}"
            )
    else:
        lines.append(f"  {C_DIM}No active resting orders on CLOB.{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    return lines


def render_dashboard(
    telemetry: dict[str, Any],
    market_info: dict[str, Any],
    start_time: float,
    clear_screen: bool = True,
) -> None:
    """Write the dashboard from a telemetry dict (never reads client secrets)."""
    elapsed = time.time() - start_time
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    lines = build_dashboard_lines(telemetry or {}, market_info, elapsed, now_str)
    if clear_screen:
        lines.insert(0, "\033[H\033[J")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def write_banner(
    *,
    dry_run: bool,
    order_version: int | None,
    signature_type: int,
    settlement_available: bool,
    neg_risk: bool,
    total_wallet: float,
    trading_bankroll: float,
    budget_pct: float,
    max_budget: float | None,
    condition_id: str,
) -> None:
    mode = (
        f"{C_YELLOW}DRY-RUN — --live was not supplied. NO real orders will be placed.{C_RESET}"
        if dry_run
        else f"{C_RED}LIVE — --live confirmed. Orders are REAL money.{C_RESET}"
    )
    lines = [
        f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}",
        f"{C_BOLD}MODE:{C_RESET} {mode}",
        f"  Condition: {condition_id} | neg_risk: {neg_risk}",
        f"  CLOB order version: {order_version if order_version is not None else 'n/a'}",
        "  Settlement path: "
        + (
            f"{C_GREEN}EOA-only CTF merge/redeem ENABLED{C_RESET}"
            if settlement_available
            else f"{C_YELLOW}CTF merge/redeem REFUSED (proxy wallet, no key, or dry-run){C_RESET}"
        ),
        f"  CLOB wallet: ${total_wallet:,.2f} | sizing cap: ${trading_bankroll:,.2f} "
        f"({budget_pct * 100.0:.1f}% of wallet"
        + (f", max ${max_budget:,.2f}" if max_budget is not None else "")
        + ") — the cap is not cash",
    ]
    if dry_run:
        lines.append(
            f"  {C_DIM}DRY-RUN: on-chain merges/redemptions are simulated; no CTF "
            "transaction is broadcast.{C_RESET}"
        )
    if neg_risk:
        lines.append(
            f"  {C_YELLOW}NEG-RISK MARKET:{C_RESET} CTF mergePositions does not settle "
            "neg-risk positions; live trading is blocked on this market."
        )
    if signature_type != 0:
        lines.append(
            f"  {C_YELLOW}ON-CHAIN SETTLEMENT UNAVAILABLE:{C_RESET} "
            f"signature_type={signature_type} (proxy/Safe). mergePositions/redeemPositions "
            "cannot be broadcast by this signer; inventory can only be exited by selling "
            "on the CLOB."
        )
    elif not dry_run and not settlement_available:
        lines.append(
            f"  {C_YELLOW}ON-CHAIN SETTLEMENT UNAVAILABLE:{C_RESET} no usable signing key; "
            "mergePositions/redeemPositions will refuse. Inventory can only be exited by "
            "selling on the CLOB."
        )
    lines.append(f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def write_final_summary(telemetry: dict[str, Any] | None) -> None:
    lines = [
        f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}",
        f"{C_BOLD}SHUTDOWN SUMMARY{C_RESET}",
    ]
    if not isinstance(telemetry, dict):
        lines.append("  Final inventory state unavailable: telemetry did not return a dict.")
        lines.append("  Do not assume the account is flat.")
        lines.append(f"{C_CYAN}================================================================{C_RESET}")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        return

    shares_yes = telemetry.get("shares_yes")
    shares_no = telemetry.get("shares_no")
    net_exposure = telemetry.get("net_exposure")
    lines.append(f"  Equity: ${_num(telemetry, 'equity', '.2f')} | free cash: ${_num(telemetry, 'free_cash', '.2f')} "
                 f"| locked cash: ${_num(telemetry, 'locked_cash', '.2f')} | cash: ${_num(telemetry, 'cash', '.2f')}")
    lines.append(f"  Positions: YES {_num(telemetry, 'shares_yes', '.2f')} / NO {_num(telemetry, 'shares_no', '.2f')} "
                 f"| complete sets {_num(telemetry, 'complete_sets', '.2f')} "
                 f"| net exposure {_num(telemetry, 'net_exposure', '+.2f')}")
    if (isinstance(shares_yes, (int, float)) and shares_yes != 0) or (
        isinstance(shares_no, (int, float)) and shares_no != 0
    ) or (isinstance(net_exposure, (int, float)) and net_exposure != 0):
        lines.append(
            f"  {C_RED}{C_BOLD}WARNING: OPEN INVENTORY REMAINS ON THE VENUE.{C_RESET} "
            "It was NOT liquidated. Exit manually by selling on the CLOB, or redeem "
            "after resolution if on-chain settlement is available."
        )
    elif not isinstance(shares_yes, (int, float)) or not isinstance(shares_no, (int, float)):
        lines.append(
            "  Position figures are n/a; the account is NOT confirmed flat."
        )
    lines.append(f"{C_CYAN}================================================================{C_RESET}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
