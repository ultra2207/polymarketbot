"""Live settlement buys: the paper pillar's sizing, executed on this venue."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

import requests

from polymarketbot.logs import get_logger
from polymarketbot.live.settlement import LiveSettlementDesk, held_exposures, load_market
from polymarketbot.strategy.latency import ActionBook, RttProbe
from polymarketbot.strategy.venue.reads import CLOB

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")


def build_settlement_desk(
    *,
    feed: Any,
    clob: Any,
    market: dict[str, Any],
    http: requests.Session,
) -> Optional[LiveSettlementDesk]:
    """The desk that sizes and lands a settlement buy for the active market.

    The decision itself is :mod:`polymarketbot.live.settlement` (the paper
    pillar's gate and tail budget). This function only supplies the live
    venue's facts: the feed's books, the CLOB taker placement, the measured
    round trip the landing queue charges, and the market's resolution cluster.

    Returns ``None`` when no CLOB round trip can be measured. The landing queue
    refuses an action it cannot give a measured flight to, so a desk without one
    could never land - and the live trader must never execute at 0 ms.
    """
    probe = RttProbe(CLOB, http)
    measured = probe.measure()
    if measured is None:
        log.error(
            "[SETTLEMENT] Desk not built: no measured CLOB round trip (GET /time "
            "failed and no persisted p50). A settlement buy would execute at 0 ms."
        )
        return None
    log.info(
        f"[SETTLEMENT] Desk ready: measured CLOB round trip p50="
        f"{measured * 1000.0:.1f}ms, sizing from the paper pillar's tail budget."
    )

    def book_reader(token_id: str) -> Optional[dict]:
        try:
            return feed.get_book_sync(token_id)
        except Exception as exc:  # noqa: BLE001 - a missing book is a refusal
            log.warning(f"[SETTLEMENT] Book read for {str(token_id)[:10]} failed: {exc}")
            return None

    def place_taker(token_id: str, price: float, shares: float) -> bool:
        # Fill-or-kill: a settlement buy that cannot be filled at the sized
        # prices must not leave a resting bid at the limit on a market that has
        # stopped trading.
        result = clob.place_order(
            token_id=token_id,
            side="BUY",
            price=price,
            size=shares,
            tick_size=float(market["tick_size"]),
            neg_risk=bool(market["neg_risk"]),
            fee_rate_bps=float(market.get("fee_rate_bps") or 0.0),
            order_type="FOK",
            post_only=False,
        )
        accepted = bool(getattr(result, "success", False))
        if not accepted:
            log.warning(
                f"[SETTLEMENT] FOK taker BUY {shares:.4f} @ {price:.4f} rejected: "
                f"{getattr(result, 'error_message', None) or 'unknown error'}"
            )
        else:
            log.info(
                f"[SETTLEMENT] FOK taker BUY accepted: order "
                f"{getattr(result, 'order_id', '') or 'n/a'} "
                f"{getattr(result, 'status', '') or ''}".rstrip()
            )
        return accepted

    def cluster_key(condition_id: str) -> Optional[str]:
        # One market per live engine: its event is its resolution source, and an
        # unknown event leaves the condition as its own (narrowest) cluster.
        if condition_id != str(market["condition_id"]):
            return None
        return str(market.get("event_id") or "") or None

    def exhaustive(condition_id: str) -> bool:
        return bool(market["neg_risk"]) if condition_id == str(market["condition_id"]) else False

    return LiveSettlementDesk.build(
        actions=ActionBook(probe.measure),
        book_reader=book_reader,
        place_taker=place_taker,
        cluster_key=cluster_key,
        exhaustive=exhaustive,
    )


def settlement_tick(desk: LiveSettlementDesk, engine: Any, market: dict[str, Any]) -> Any:
    """One settlement pass on the live engine's tick thread.

    The venue's own market row is the input (``venue.parse_market`` is the
    trading gate: a market that no longer accepts orders has no book to buy),
    the engine's free collateral is the hard spending limit, the inventory held
    against the verdict is charged to the tail budget as prior exposure, and the
    measured reward yield is the capital hurdle the settlement tranche must beat.
    """
    venue_market = load_market(str(market["condition_id"]))
    if venue_market is None:
        log.info(
            f"[SETTLEMENT] {str(market['condition_id'])[:10]} has no tradable market "
            "row (closed, archived or unreadable): no settlement buy can be sized."
        )
        return None
    state = engine.get_telemetry()
    equity = float(state.get("equity") or 0.0)
    daily_yield = float(engine.get_current_daily_yield() or 0.0)
    shadow_yield = daily_yield / equity if equity > 0.0 else None
    claimed = desk.claimed_token(venue_market)
    prior = held_exposures(
        condition_id=venue_market.condition_id,
        token_ids=venue_market.token_ids,
        shares=(
            float(state.get("shares_yes") or 0.0),
            float(state.get("shares_no") or 0.0),
        ),
        cost_usd=(
            float(state.get("shares_yes") or 0.0) * float(state.get("avg_cost_yes") or 0.0),
            float(state.get("shares_no") or 0.0) * float(state.get("avg_cost_no") or 0.0),
        ),
        claimed_token_id=claimed,
    )
    decision = desk.plan(
        markets=[venue_market],
        cash_usd=float(engine.get_cash()),
        prior=prior,
        equity_usd=equity,
        shadow_yield=shadow_yield,
    )
    log.info(decision.describe())
    return decision


async def settlement_pass(ctx: "RunContext") -> None:
    """One settlement pass, run from the market-lifecycle cadence."""
    # Settlement buys: a resolved-but-unfinalised market is the one
    # this engine has no sizing for anywhere else, so the paper
    # pillar's gate and tail budget run here, once quoting has
    # halted, and the order lands one measured round trip later.
    if ctx.market_halt_done:
        if ctx.settlement_desk is None and not ctx.settlement_desk_refused:
            ctx.settlement_desk = await asyncio.to_thread(
                build_settlement_desk,
                feed=ctx.feed,
                clob=ctx.clob_client,
                market=ctx.market,
                http=ctx.l2_session,
            )
            ctx.settlement_desk_refused = ctx.settlement_desk is None
        if ctx.settlement_desk is not None and not ctx.settlement_desk.pending():
            try:
                decision = await asyncio.to_thread(
                    settlement_tick, ctx.settlement_desk, ctx.engine, ctx.market
                )
            except Exception as exc:
                decision = None
                log.error(f"[SETTLEMENT] Decision pass failed: {exc}")
            # A steady state is a state, not an event: log on change.
            if decision is not None:
                line = decision.describe()
                if line != ctx.settlement_note:
                    ctx.settlement_note = line
                    log.info(f"[SETTLEMENT] {line}")
