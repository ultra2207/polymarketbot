"""Landing side of the archived CTF strategy (formerly in ``paper.actions``).

The live paper engine no longer routes CTF actions: the strategy was archived
on measured data (see ``README.md``).  This module keeps the execution half the
archived runner reached - the four action kinds its loop submits - so the
decision loop and its landing can still be exercised together in research.

It is a verbatim move of the CTF branches of ``polymarketbot.paper.actions``:
``ctf_cancel``/``ctf_quote`` maintain the resting pair, ``ctf_taker`` re-prices
both legs against the book that exists when it lands and queues the merge, and
``ctf_merge`` books the on-chain merge.  Nothing calls it from the live package;
re-wire it into a landing pass to re-run the strategy.
"""

from __future__ import annotations

from typing import Any

from polymarketbot.logs import get_logger

from . import pillars as ctf

log = get_logger("archive.ctf")


def land_ctf(engine: Any, books: dict[str, dict]) -> None:
    """Land every due CTF action, in submission order."""
    for action in engine.actions.due():
        payload = action.payload
        if action.kind == "ctf_cancel":
            engine.maker_quotes.pop(payload["token_id"], None)
        elif action.kind == "ctf_quote":
            engine.maker_quotes[payload["token_id"]] = payload["resting"]
        elif action.kind == "ctf_taker":
            market = engine.by_condition.get(payload["condition_id"])
            fresh = (engine.feed.books(market.token_ids, force=True)
                     if market is not None else books)
            land_ctf_taker(engine, action.strategy, payload, fresh)
        elif action.kind == "ctf_merge":
            engine.ledger.merge(
                action.strategy,
                condition_id=payload["condition_id"],
                token_id=payload["token_id"],
                pair_shares=payload["pair_shares"],
                gas=payload["gas"],
            )


def land_ctf_taker(engine: Any, strategy: str, payload: dict,
                   books: dict[str, dict]) -> None:
    """Buy both legs of a parity tranche against the landing book, then merge."""
    market = engine.by_condition.get(payload["condition_id"])
    if market is None or not market.binary:
        return
    yes, no = market.token_ids
    tranches = ctf.optimize_taker_parity(
        market, books.get(yes) or {}, books.get(no) or {},
        engine.ledger.cash(strategy), engine.gas_usd or 0.0,
    )
    for tranche in tranches:
        if engine.ledger.buy(strategy, condition_id=market.condition_id, token_id=yes,
                             shares=tranche.shares, price=tranche.price_yes,
                             fee=tranche.fee_yes, tag="ctf_leg"):
            if engine.ledger.buy(strategy, condition_id=market.condition_id, token_id=no,
                                 shares=tranche.shares, price=tranche.price_no,
                                 fee=tranche.fee_no, tag="ctf_leg"):
                engine._fills[strategy] += 1
                log.info(f"ctf parity {tranche.shares:.1f} sh "
                         f"@{tranche.price_yes:.4f}+{tranche.price_no:.4f} "
                         f"edge=${tranche.edge_usd:.4f}")
                if engine.gas_usd is not None:
                    try:
                        onchain = ctf.resolve_on_chain_timing("merge")
                    except ctf.OnChainRefused as exc:
                        log.warning(f"{exc}; parity legs stay as inventory")
                        continue
                    engine.actions.submit("ctf_merge", strategy, {
                        "condition_id": market.condition_id,
                        "token_id": yes,
                        "pair_shares": tranche.shares,
                        "gas": tranche.gas,
                    }, rtt=onchain.flight_s)
