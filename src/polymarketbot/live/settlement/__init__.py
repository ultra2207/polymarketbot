"""Live settlement buys: the paper pillar's verdict gate and tail budget, on the venue.

Before this package the live trader had **no settlement sizing at all**. It merged
complete sets it already held (``live/engine/merge.py``), redeemed its own
inventory after the market closed (``--redeem-on-close``), and never bought a
resolved-but-unfinalised position. The paper pillar that is being audited sizes
that buy with two shared modules:

* :mod:`polymarketbot.strategy.settlement.decision` - the verdict gate
  (``verdict_for`` refuses without an oracle proposal, ``edge_inputs`` /
  ``fundable_legs`` price the ask against that verdict);
* :mod:`polymarketbot.strategy.settlement.allocator` - the tail budget
  (``optimize_tail_budget`` maximises expected profit inside the measured
  expected shortfall, at the appetite and band stated in ``configs/risk.yaml``).

This package is *that same decision* for the live trader. It re-implements
nothing: the gate is ``decision``, the dollars are the allocator, the appetite
and band come from the paper path's own loader
(:mod:`polymarketbot.paper.settlement_risk`, the single named place that reads
``configs/risk.yaml``), the measured tail comes from
:mod:`polymarketbot.strategy.settlement.tail_evidence`, and the gas cost comes
from the measured on-chain path (:mod:`polymarketbot.strategy.onchain.ctf.gas`).

Two refusals are inherited, not softened:

* **No oracle proposal, no price.** ``verdict_for`` returns ``None`` and the
  candidate is refused. A book mid is never substituted for the oracle's value.
* **No measured gas or POL/USD, no order.** Both inputs must resolve; the
  measured probes are called with an explicit unmeasured sentinel so "the
  measurement failed" is a ``None`` here, never an invented Gwei or dollar. An
  unpriced on-chain exit must not be treated as free, and the action refuses.

The order itself is not executed where the decision is made: it is queued on
:class:`polymarketbot.strategy.latency.ActionBook` and lands one measured CLOB
round trip later, against the book that exists at landing time (re-read, and
re-gated, in :meth:`LiveSettlementDesk.land_due`).

The package's modules, and what each owns:

* ``measure``: the measured inputs (``measure_gas_usd``, ``load_market``, the
  ask ladder);
* ``models``: ``PricedMarket``, ``SettlementGrant``, ``SettlementDecision``,
  ``Landing``;
* ``decision``: the gate and the sizing (``decide``, ``held_exposures``) and the
  refusal reasons it states;
* ``desk``: ``LiveSettlementDesk``, which plans on the tick and lands a measured
  round trip later.

Every name that used to live in ``live/settlement.py`` is re-exported here, so
``from polymarketbot.live.settlement import ...`` keeps working unchanged.
"""

from __future__ import annotations

from .decision import (
    BELOW_SHADOW_FLOOR,
    GAS_UNMEASURED,
    MARKET_NOT_TRADABLE,
    NEGATIVE_EDGE,
    NO_APPETITE,
    NO_ASK,
    NO_EDGE,
    NO_GRANT,
    NO_RTT,
    NO_TAIL,
    NO_VERDICT,
    decide,
    held_exposures,
)
from .desk import STRATEGY, LiveSettlementDesk
from .measure import load_market, log, measure_gas_usd
from .models import Landing, PricedMarket, SettlementDecision, SettlementGrant

__all__ = [
    # measured inputs
    "measure_gas_usd",
    "load_market",
    # models
    "PricedMarket",
    "SettlementGrant",
    "SettlementDecision",
    "Landing",
    # the decision
    "decide",
    "held_exposures",
    # the desk
    "LiveSettlementDesk",
    "STRATEGY",
    # the package's one logger
    "log",
    # refusal reasons
    "NO_VERDICT",
    "NO_ASK",
    "NO_EDGE",
    "NEGATIVE_EDGE",
    "MARKET_NOT_TRADABLE",
    "GAS_UNMEASURED",
    "NO_APPETITE",
    "NO_TAIL",
    "NO_GRANT",
    "BELOW_SHADOW_FLOOR",
    "NO_RTT",
]
