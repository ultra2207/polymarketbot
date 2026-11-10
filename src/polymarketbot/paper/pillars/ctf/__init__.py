"""CTF complete-set parity: buy both legs under par, or quote both bids over par.

The implementation is split by concern: :mod:`.fill_model` (measured horizon
fill probability), :mod:`.markout` (measured single-leg markout), :mod:`.parity`
(quote records, shared book arithmetic and the quote audit), :mod:`.taker`
(taker complete-set parity), :mod:`.maker` (maker two-sided quoting) and
:mod:`.onchain` (split/merge/redeem leg clock and the complete-set count).
Every name this pillar exported is re-exported here; module-level mutable
state (``_QUOTE_AUDIT``, the warn-dedupe sets, the loaded models) and the
``logger`` a function actually writes through live in the submodule that reads
them, so patch them there (``ctf.parity._QUOTE_AUDIT``, ``ctf.maker.logger``),
never here: a re-exported name in this module is a snapshot, not a live binding.

A binary market's two outcome tokens form a complete set: one YES share plus one
NO share redeems for exactly ``1.00`` USDC.  The taker optimizer therefore walks
both ask ladders together and buys the largest size whose marginal edge

    1 - (price_yes + price_no) - fee_yes - fee_no - gas/share

is still positive.

The maker optimizer is **not** an arbitrage: resting one bid on each leg is two
independent market-making bets.  The ``1 - bid_yes - bid_no`` sum is a *quote*,
not a fill, and it is below par for essentially every two-sealed market (a
recorded day of L2 tape: 20,217 of 20,221 condition snapshots).  It is realized
only when both legs fill; a single-leg fill leaves naked directional inventory
that the ledger marks at the best bid, i.e. at ``quote - tick``.  The optimizer
therefore prices the pair as the expected value of that three-outcome bet:

    E[P&L per set] = p_y*p_n*(1 - bid_yes - bid_no + rebate_y + rebate_n)
                    + p_y*(1-p_n)*(rebate_y - unwind_cost_y)
                    + p_n*(1-p_y)*(rebate_n - unwind_cost_n)

and emits only when that expectation is strictly positive.  ``p_y``/``p_n`` are
the **horizon-matched** measured fill probabilities from
``data/derived/ctf_quote_fill_horizon.json``: a quote is sampled on the engine's
own tick grid over the recorded L2 tape and counts as filled when a SELL print
crosses it within the requote lifetime.  That is a joint probability (arrival
*and* crossing); the print-conditional ``fill_model.json`` is conditional on a
print having happened and therefore cannot be used here — over a one-tick
lifetime it overstates a per-leg fill by more than an order of magnitude.
``unwind_cost`` is the measured distance from the quote back to the executable
best bid (the same mark the ledger uses); ``rebate`` is the market's measured
maker rebate.  With no horizon measurement loadable the expected value is
unknown and no quote is emitted.

Apart from the cached measurements, every function is a pure function of its
arguments: the books, the market's own fields (``tick``, ``min_order``,
``fee_schedule``) and the caller's cash/gas.  Nothing here invents a rate,
threshold or price.

Units: prices are fractions of ``1.00`` USDC per share, shares are outcome
shares, cash, fees, gas and edges are USDC.  A complete set is one share of
every distinct token of the condition, so its redemption value is ``1.00``.
"""

from __future__ import annotations

from polymarketbot.fees import FeeSchedule, maker_fee_rebate, taker_fee

from . import fill_model, maker, markout, onchain, parity, taker
from .fill_model import (
    DEFAULT_QUOTE_FILL_HORIZON_PATH,
    QuoteFillHorizon,
    load_quote_fill_horizon,
)
from .maker import optimize_maker_parity
from .markout import (
    DEFAULT_MAKER_MARKOUT_PATH,
    MakerMarkout,
    load_maker_markout,
)
from .onchain import (
    OnChainRefused,
    OnChainTiming,
    merge_pairs,
    require_on_chain_timing,
    resolve_on_chain_timing,
)
from .parity import (
    QUOTE_AUDIT_INTERVAL_S,
    MakerParityQuote,
    ParityTranche,
    log_quote_audit,
)
from .taker import optimize_taker_parity

__all__ = [
    "DEFAULT_MAKER_MARKOUT_PATH",
    "DEFAULT_QUOTE_FILL_HORIZON_PATH",
    "QUOTE_AUDIT_INTERVAL_S",
    "FeeSchedule",
    "MakerMarkout",
    "MakerParityQuote",
    "OnChainRefused",
    "OnChainTiming",
    "ParityTranche",
    "QuoteFillHorizon",
    "fill_model",
    "load_maker_markout",
    "load_quote_fill_horizon",
    "log_quote_audit",
    "maker",
    "maker_fee_rebate",
    "markout",
    "merge_pairs",
    "onchain",
    "optimize_maker_parity",
    "optimize_taker_parity",
    "parity",
    "require_on_chain_timing",
    "resolve_on_chain_timing",
    "taker",
    "taker_fee",
]
