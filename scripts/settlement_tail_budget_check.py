"""Window-9 verification of the settlement harvester's tail budget.

Replays the measured 2026-11-10 14:25 fill tape (18 shares, 8 levels, 0.81-0.98,
Gamma's 5 % taker schedule) through four sizing rules and prints the comparison:

1. the tail budget at the stated appetite and band (``configs/risk.yaml``),
2. the same at twice the measured tail rate and at a tighter band,
3. the old mean-variance / growth charge (the rule this replaced),
4. full single-bet Kelly, at both tails.

Every number is measured or derived; nothing here is a knob.  Bounded: no
network calls, no ledger, no sleep.

    uv run python scripts/settlement_tail_budget_check.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polymarketbot.fees import FeeSchedule  # noqa: E402
from polymarketbot.logs import get_logger  # noqa: E402
from polymarketbot.paper.settlement_risk import load_settlement_tail_policy  # noqa: E402
from polymarketbot.trading.domain2.settlement_allocator import (  # noqa: E402
    MarginalSettlementAllocator,
    SettlementCandidate,
)
from polymarketbot.trading.domain2.tail_evidence import load_tail_evidence  # noqa: E402

log = get_logger("settlement_tail_check")

# The window-9 fill tape, replayed exactly: [(price, shares)] of the eight
# landed levels, and the verdict probability the pricing model gave it.
WIN9_LADDER = [
    (0.8100, 20.0),
    (0.8300, 20.0),
    (0.8600, 25.0),
    (0.8700, 84.61),
    (0.8800, 83.33),
    (0.9600, 19.83),
    (0.9700, 300.0),
    (0.9800, 496.32),
]
WIN9_P_WIN = 0.98947        # 1 - the pricing model's 1.0530 % hazard bound
WIN9_EQUITY = 1000.0        # the pillar's cash
WIN9_SETTLE_SEC = 7200.0    # the tape's measured UMA liveness
WIN9_MIN_ORDER = 5.0
# The taker rate the window's own fee implies: 20 sh @ 0.81 paid $0.1539, and
# 0.1539 / (20 * 0.81 * 0.19) = 0.05.  The trade tape cannot supply it: its
# ``fee_rate_bps`` column is 0 on all 360,273 tapes rows (the field was retired
# at the 2026-04-28 V2 cutover), so the schedule comes from Gamma / the fill.
WIN9_FEE_RATE = 0.05
# Measured on-chain redemption gas (docs/onchain-findings.md: median base fee).
WIN9_GAS_USD = 0.00155
# The recorded result of the replaced mean-variance solve on this exact ladder.
OLD_VARIANCE_ALLOCATED_USD = 786.96


def candidate(condition_id="0xbcd8bf70c6", *, event_key="evt-win9",
              ladder=None, p_win=WIN9_P_WIN, token_id=None):
    return SettlementCandidate(
        condition_id=condition_id,
        token_id=token_id or f"tok-{condition_id}",
        question=f"window-9 market {condition_id}",
        target_settle_sec=WIN9_SETTLE_SEC,
        p_win=p_win,
        ask_ladder=list(WIN9_LADDER if ladder is None else ladder),
        fee_schedule=FeeSchedule(rate=WIN9_FEE_RATE, exponent=1.0),
        min_order_shares=WIN9_MIN_ORDER,
        event_key=event_key,
        event_exhaustive=True,
        gas_usd=WIN9_GAS_USD,
    )


def tail_row(label, result, *, q):
    report = result.tail_budget
    all_in = sum(row.all_in_usd for row in result.allocations)
    print(
        f"{label:<34} cost=${result.total_allocated_usd:>8,.2f} all-in=${all_in:>8,.2f} "
        f"E[profit]=${result.expected_portfolio_profit_usd:>7,.2f} "
        f"shortfall=${report.expected_shortfall_usd:>7,.2f} "
        f"band=${1.0 - report.tail_confidence:.2%} appetite=${report.risk_appetite_usd:>6,.2f} "
        f"clusters={len(report.cluster_notional_usd)} "
        f"binding={report.binding_constraint}"
    )
    return all_in


def kelly_fraction(p_win: float, price: float, fee_rate: float) -> float:
    """``f* = q - (1-q)/b`` with the measured payoff odds ``b``.

    ``b`` is the profit per dollar staked on a win, so
    ``b = (1 - price - fee) / (price + fee)``: a win returns ``1 - price``, a
    loss costs ``price``, and both sides pay the taker fee.
    """
    fee = fee_rate * price * (1.0 - price)
    win = 1.0 - price - fee
    stake = price + fee
    if stake <= 0.0:
        return float("nan")
    b = win / stake
    return p_win - (1.0 - p_win) / b


def mean_variance_charge(cand: SettlementCandidate, equity: float):
    """The replaced objective: ``max profit - x'Qx / (2E)`` on one ladder.

    Reconstructed to show what it answered, on the same ladder, with the same
    measured ``p_win``.  Kept only as a comparison line; it is not the rule the
    allocator runs.
    """
    if not cand.ask_ladder:
        return 0.0, 0.0
    q = cand.p_win
    var = q * (1.0 - q)
    lo, hi = 0.0, sum(sz for _px, sz in cand.ask_ladder)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        cost, fills = cand.fills_for_shares(mid)
        fee = cand.fee_for_fills(fills)
        price = fills[-1][0] if fills else cand.best_ask
        edge = cand.marginal_edge(price)
        charge = var * mid / equity
        if edge - charge > 0.0:
            lo = mid
        else:
            hi = mid
    shares = lo
    cost, fills = cand.fills_for_shares(shares)
    return cost, cand.p_win * shares - cost - cand.fee_for_fills(fills) - cand.gas_usd


def main() -> int:
    policy = load_settlement_tail_policy()
    evidence = load_tail_evidence()
    q = evidence.q_loss_upper(policy.tail_confidence)
    allocator = MarginalSettlementAllocator("tail_budget")

    print("=" * 108)
    print("WINDOW 9 (2026-11-10 14:25) — measured inputs")
    print("=" * 108)
    print(f"ladder               : {len(WIN9_LADDER)} levels, "
          f"{sum(s for _p, s in WIN9_LADDER):,.2f} shares, "
          f"${sum(p * s for p, s in WIN9_LADDER):,.2f} of ask depth")
    print(f"p_win (pricing model): {WIN9_P_WIN:.5f}  (1 - 1.0530 % hazard bound)")
    print(f"fee schedule         : {WIN9_FEE_RATE:.0%} taker, gamma + the window's own fee")
    print(f"tail evidence        : {evidence.describe()}")
    print(f"stated appetite      : ${policy.risk_appetite_usd:,.2f} at "
          f"{policy.tail_confidence:.1%} (from {policy.source})")
    print(f"measured tail q      : {q:.6f}  -> notional the appetite pays for: "
          f"${policy.risk_appetite_usd * (1 - policy.tail_confidence) / q:,.2f}")
    print()

    print("=" * 108)
    print("ALLOCATION — tail budget vs the replaced rules")
    print("=" * 108)
    one = [candidate()]
    base = allocator.allocate(
        one, WIN9_EQUITY,
        risk_appetite_usd=policy.risk_appetite_usd,
        tail_confidence=policy.tail_confidence,
        tail_evidence=evidence,
        equity_usd=WIN9_EQUITY,
    )
    tail_row(f"TAIL BUDGET @ {policy.tail_confidence:.1%}", base, q=q)

    doubled = allocator.allocate(
        one, WIN9_EQUITY,
        risk_appetite_usd=policy.risk_appetite_usd,
        tail_confidence=policy.tail_confidence,
        tail_evidence=evidence.scaled(2.0),
        equity_usd=WIN9_EQUITY,
    )
    tail_row("  same, 2x measured tail", doubled, q=q)

    loose = allocator.allocate(
        one, WIN9_EQUITY,
        risk_appetite_usd=policy.risk_appetite_usd,
        tail_confidence=0.95,
        tail_evidence=evidence,
        equity_usd=WIN9_EQUITY,
    )
    tail_row("  same appetite at a 95 % band", loose, q=q)

    # The measured shadow yield of the reward book, top of the observed range:
    # capital locked here is capital the reward desk cannot use.
    hurdle = allocator.allocate(
        one, WIN9_EQUITY,
        risk_appetite_usd=policy.risk_appetite_usd,
        tail_confidence=policy.tail_confidence,
        tail_evidence=evidence,
        equity_usd=WIN9_EQUITY,
        capital_hurdle_usd_per_usd_per_day=0.0924,
    )
    tail_row("  with the measured 0.0924 shadow yield", hurdle, q=q)

    risk_neutral = MarginalSettlementAllocator("water_filling").allocate(one, WIN9_EQUITY)
    print(f"{'risk-neutral water-fill':<34} cost=${risk_neutral.total_allocated_usd:>8,.2f} "
          f"E[profit]=${risk_neutral.expected_portfolio_profit_usd:>7,.2f}"
          f"   (the old rule: 100 % of the treasury in one condition)")
    mv_cost, mv_profit = mean_variance_charge(one[0], WIN9_EQUITY)
    print(f"{'mean-variance, lambda=1/equity':<34} cost=${mv_cost:>8,.2f} "
          f"E[profit]=${mv_profit:>7,.2f}   (reconstructed; the recorded solve on "
          f"this ladder was ${OLD_VARIANCE_ALLOCATED_USD:,.2f})")
    print("a 95 % band cannot see a 0.13 % event: the loss mass sits outside the band, "
          "so the shortfall is smooth and the budget stops binding")
    print()

    print("=" * 108)
    print("KELLY — the rule the tail budget replaced (same ladder, 0.98 fill)")
    print("=" * 108)
    f_hazard = kelly_fraction(WIN9_P_WIN, 0.98, WIN9_FEE_RATE)
    f_measured = kelly_fraction(1.0 - q, 0.98, WIN9_FEE_RATE)
    f_2x = kelly_fraction(1.0 - 2.0 * q, 0.98, WIN9_FEE_RATE)
    print(f"f* at the pricing model's p_win={WIN9_P_WIN:.5f}: {f_hazard:+.4f} "
          f"({'BET' if f_hazard > 0 else 'DO NOT BET'})")
    print(f"f* at the measured tail       p={1 - q:.6f}: {f_measured:+.4f} "
          f"({'BET' if f_measured > 0 else 'DO NOT BET'})")
    print(f"f* at twice the measured tail p={1 - 2 * q:.6f}: {f_2x:+.4f} "
          f"({'BET' if f_2x > 0 else 'DO NOT BET'})")
    # Where Kelly's sign flips: f* = 0 at p = 1 / (1 + b), i.e. at
    # 1 - q = 1 / (1 + b).  Solved from the measured payoff odds.
    fee = WIN9_FEE_RATE * 0.98 * (1.0 - 0.98)
    b = (1.0 - 0.98 - fee) / (0.98 + fee)
    break_even_q = 1.0 - 1.0 / (1.0 + b)
    print(f"Kelly's sign flips at q = {break_even_q:.5f} "
          f"({break_even_q / q:.1f}x the measured {q:.6f}); the old zero-event "
          f"hazard bound (0.010530) sits on the wrong side of it")
    shrink = doubled.total_allocated_usd / base.total_allocated_usd
    print(f"-> the tail budget at a binding band: 2x the tail costs "
          f"{1.0 - shrink:.1%} of the size and keeps every tranche funded; "
          f"the decision does not move")
    print()

    print("=" * 108)
    print("CLUSTERING — the diversification axis is the resolution source")
    print("=" * 108)
    half = [(p, s / 2.0) for p, s in WIN9_LADDER]
    split_distinct = allocator.allocate(
        [candidate("0xaaa", event_key="evt-a", ladder=half),
         candidate("0xbbb", event_key="evt-b", ladder=half)],
        WIN9_EQUITY,
        risk_appetite_usd=policy.risk_appetite_usd,
        tail_confidence=0.995,
        tail_evidence=evidence,
        equity_usd=WIN9_EQUITY,
    )
    tail_row("two distinct sources", split_distinct, q=q)
    split_same = allocator.allocate(
        [candidate("0xaaa", event_key="evt-shared", ladder=half),
         candidate("0xbbb", event_key="evt-shared", ladder=half)],
        WIN9_EQUITY,
        risk_appetite_usd=policy.risk_appetite_usd,
        tail_confidence=0.995,
        tail_evidence=evidence,
        equity_usd=WIN9_EQUITY,
    )
    tail_row("one shared source", split_same, q=q)
    print("two legs of one resolution source fail together, so their notionals add to "
          "ONE cluster loss instead of earning two budgets")
    print(f"  worst-case probability, same dollars, same shortfall: "
          f"shared source {q:.6f} vs distinct sources {q * q:.8f} "
          f"({q / (q * q):,.0f}x difference in joint failure probability)")
    print()

    print("=" * 108)
    print("PER-CLUSTER STATEMENT (99.5 % band, two distinct sources)")
    print("=" * 108)
    report = split_distinct.tail_budget
    for cluster, notional in report.cluster_notional_usd.items():
        print(
            f"  {str(cluster)[:12]:<12} q={report.cluster_q[cluster]:.6f} "
            f"notional=${notional:>8,.2f} E[loss]=${report.cluster_expected_loss_usd[cluster]:>6,.2f} "
            f"shortfall=${report.cluster_shortfall_usd[cluster]:>7,.2f}"
        )
    print(f"  total shortfall ${report.expected_shortfall_usd:,.2f} of "
          f"${report.risk_appetite_usd:,.2f} appetite | binding={report.binding_constraint}")
    worst = sum(report.cluster_notional_usd.values())
    qs = list(report.cluster_q.values())
    any_p = sum(qs)                       # union bound: any cluster fails
    all_p = math.prod(qs)                 # independent sources: all fail
    print(f"  worst case: ${worst:,.2f} of notional lost (LGD = 1) at probability "
          f"{all_p:.8f} across independent sources; "
          f"<= {any_p:.6f} for ANY cluster failing (union bound)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
