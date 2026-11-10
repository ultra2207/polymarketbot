"""Closed-form ladder consumption and marginal-velocity mathematics."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ._shared import SECONDS_PER_YEAR
from .types import SettlementCandidate


class LadderMathMixin:
    """Exact ladder primitives shared by every allocator backend."""

    # =========================================================================
    # Closed-Form Mathematical Primitives
    # =========================================================================

    @staticmethod
    def compute_shares_and_cost(
        ask_ladder: Sequence[Tuple[float, float]],
        capital: float,
    ) -> Tuple[float, float, float, float, int, List[Tuple[float, float, float]]]:
        """Calculates exact orderbook ladder consumption for a given capital amount.

        Returns:
            Tuple: (shares, cost_usd, effective_avg_price, marginal_price, levels_consumed, level_fills)
        """
        if capital <= 0 or not ask_ladder:
            return 0.0, 0.0, 0.0, 0.0, 0, []

        rem_capital = float(capital)
        total_shares = 0.0
        total_cost = 0.0
        levels_consumed = 0
        marginal_px = ask_ladder[0][0]
        level_fills: List[Tuple[float, float, float]] = []

        for px, sz in ask_ladder:
            if rem_capital <= 1e-9:
                break
            level_cap = px * sz
            marginal_px = px
            if rem_capital >= level_cap:
                total_shares += sz
                total_cost += level_cap
                rem_capital -= level_cap
                levels_consumed += 1
                level_fills.append((px, sz, level_cap))
            else:
                shs = rem_capital / px
                total_shares += shs
                total_cost += rem_capital
                level_fills.append((px, shs, rem_capital))
                rem_capital = 0.0
                levels_consumed += 1
                break

        eff_px = total_cost / total_shares if total_shares > 1e-9 else marginal_px
        return total_shares, total_cost, eff_px, marginal_px, levels_consumed, level_fills

    @classmethod
    def fill_at_landing(
        cls,
        ask_ladder: Sequence[Tuple[float, float]],
        max_cost_usd: float,
        *,
        min_order_shares: float = 0.0,
        limit_price: Optional[float] = None,
    ) -> Tuple[float, float, float, List[Tuple[float, float, float]], str]:
        """Fill a queued taker buy against the ask ladder that exists at landing.

        The decision-time ladder is only an estimate: by the time the order
        lands the book has moved. This consumes the *landing* ladder up to the
        planned cost, clamps to the depth that is actually resting, and refuses
        the fill outright when it is below the venue minimum or when its VWAP
        exceeds the all-in price limit implied by ``p_win`` and the fee schedule.

        Returns ``(shares, cost_usd, vwap, level_fills, unfilled_reason)``; an
        unfilled order returns zero size with a non-empty reason and must not
        move cash or inventory.

        Levels above ``limit_price`` are not marketable, so they are removed from
        the ladder *before* consumption rather than invalidating the whole order:
        a resting book that is cheap at the top and expensive deeper used to
        reject a fill that the top levels could have supported (every such
        rejection is a missed positive-EV tranche).
        """
        ladder: Sequence[Tuple[float, float]] = ask_ladder
        if not ask_ladder:
            return 0.0, 0.0, 0.0, [], "no resting ask depth at landing"
        if limit_price is not None:
            lim = float(limit_price)
            ladder = [(px, sz) for px, sz in ask_ladder if px <= lim + 1e-12]
            if not ladder:
                return (
                    0.0,
                    0.0,
                    0.0,
                    [],
                    f"no resting ask at or below the limit ${lim:.4f}",
                )
        shares, cost, _avg, _marg, _levels, fills = cls.compute_shares_and_cost(
            ladder, max_cost_usd
        )
        if shares <= 0.0 or cost <= 0.0:
            return 0.0, 0.0, 0.0, [], "no resting ask depth at landing"
        vwap = cost / shares
        if shares + 1e-9 < max(0.0, float(min_order_shares)):
            return (
                0.0,
                0.0,
                0.0,
                [],
                f"landing fill {shares:.4f} shs below venue minimum {min_order_shares:.4f}",
            )
        if limit_price is not None and vwap > float(limit_price) + 1e-12:
            return (
                0.0,
                0.0,
                0.0,
                [],
                f"landing VWAP ${vwap:.4f} exceeds limit ${float(limit_price):.4f}",
            )
        return shares, cost, vwap, fills, ""

    @classmethod
    def marginal_velocity(
        cls,
        price: float,
        duration_sec: float,
        fee_per_share: float = 0.0,
        p_win: float = 1.0,
        risk_price_per_usd: float = 0.0,
    ) -> float:
        """Calculates instantaneous marginal velocity d(V*C)/dC at price P.

        Formula:
            mu(P) = ((p_win - P - fee_per_share) / P) * (Sec_Per_Year / Duration_Sec)

        ``risk_price_per_usd`` is the tail budget's shadow price and is charged
        on the all-in dollar the order debits, ``P + fee_per_share``.  The
        denominator stays the *cost* ``P``: velocity is profit per dollar
        invested, and the fee is part of what the failure branch loses, not part
        of the investable dollar.
        """
        p = max(0.001, min(0.9999, float(price)))
        tau = max(1.0, float(duration_sec))
        fee = float(fee_per_share)
        net_edge = float(p_win) - p - fee - float(risk_price_per_usd) * (p + fee)
        return (net_edge / p) * (SECONDS_PER_YEAR / tau)

    @classmethod
    def candidate_profit(
        cls,
        candidate: SettlementCandidate,
        capital: float,
    ) -> Tuple[float, float, float]:
        """Calculates (net_profit_usd, annualized_arr, marginal_velocity) for a candidate given capital C.

        Formula:
            Profit(C) = p_win * Shares(C) * 1.000 - Cost(C) - Fees(C) - Gas
            ARR(C) = (Profit(C) / C) * (Sec_Per_Year / Duration_Sec)
        """
        if capital <= 0 or not candidate.ask_ladder:
            return 0.0, 0.0, 0.0

        shs, cost, eff_px, marg_px, _, fills = cls.compute_shares_and_cost(candidate.ask_ladder, capital)
        gas = candidate.gas_usd
        fee = candidate.fee_for_fills(fills)
        net_profit = candidate.p_win * shs - cost - fee - gas

        arr = (net_profit / cost) * (SECONDS_PER_YEAR / candidate.target_settle_sec) if cost > 0 else 0.0
        marg_vel = cls.marginal_velocity(
            marg_px,
            candidate.target_settle_sec,
            candidate.per_share_fee(marg_px),
            candidate.p_win,
        )
        return round(net_profit, 5), round(arr, 4), round(marg_vel, 4)
