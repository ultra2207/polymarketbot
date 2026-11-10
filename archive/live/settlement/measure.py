"""Measured inputs for a live settlement buy: the gas, the market, the ladder.

Every probe here returns ``None``/``[]`` rather than a substitute. An unpriced
on-chain exit is not free, and a market that has stopped accepting orders has no
settlement *buy* to size; both are refusals the decision states, not defaults it
invents.
"""

from __future__ import annotations

from typing import Any, Optional

from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.ctf.gas import (
    estimate_merge_cost_usd,
)
from polymarketbot.strategy.settlement.hazard import DEFAULT_CTF_REDEEM_GAS_UNITS
from polymarketbot.strategy.venue.reads import Market, parse_market

#: The package's single logger, under the name *and* file it has always been
#: bound to (``logs/polymarketbot.live.settlement.log``): the desk shares this
#: one object rather than taking a second logger, so no settlement line changes
#: its label, its file or - through a second matching sink - its line count.
log = get_logger(
    "polymarketbot.live.settlement", file="polymarketbot.live.settlement"
)


# ---------------------------------------------------------------------------
# Measured gas
# ---------------------------------------------------------------------------
def measure_gas_usd(
    *,
    gas_units: int = DEFAULT_CTF_REDEEM_GAS_UNITS,
    chain_client: Any = None,
) -> Optional[float]:
    """One on-chain settlement action's cost in USD, or ``None`` when unmeasured.

    Delegates to the single measured path in
    :mod:`polymarketbot.strategy.onchain.ctf.gas`, which returns ``None`` rather
    than substituting a baseline. ``None`` is a refusal: an unpriced exit is not
    free, and the live trader must not size a position whose redemption cost it
    guessed.
    """
    usd = estimate_merge_cost_usd(
        gas_units=int(gas_units), chain_client=chain_client
    )
    if usd is None:
        log.warning(
            "Settlement gas refused: no measured Polygon gas price and POL/USD"
        )
    return usd


def load_market(condition_id: str, *, gamma: Optional[GammaClient] = None) -> Optional[Market]:
    """The tradable market for ``condition_id``, or ``None``.

    ``venue.parse_market`` is the venue's own trading gate: a market that has
    stopped accepting orders cannot be bought at any price, so there is no
    settlement *buy* to size. Returning ``None`` says exactly that instead of
    pricing a book that does not exist.
    """
    client = gamma or GammaClient()
    try:
        row = client.market(condition_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable venue is a refusal
        log.warning(f"Settlement market lookup for {condition_id[:10]} failed: {exc}")
        return None
    if not isinstance(row, dict) or not row:
        return None
    return parse_market(row)


def _ask_ladder(book: Optional[dict]) -> list[tuple[float, float]]:
    """``(price, shares)`` ask levels from a feed snapshot or a paper-shaped book.

    Accepts the feed's ``snapshot.asks`` (objects, or ``(price, size)`` pairs) or
    a dict with an ``asks`` list of dicts, so the desk reads the same structure
    the paper pillar's ``books.ladder`` reads without a second venue parser.
    """
    if book is None:
        return []
    levels = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
    out: list[tuple[float, float]] = []
    for level in levels or []:
        try:
            if isinstance(level, dict):
                price, size = float(level.get("price")), float(level.get("size"))
            elif hasattr(level, "price"):
                price, size = float(level.price), float(level.size)
            else:
                price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            out.append((price, size))
    out.sort(key=lambda item: item[0])
    return out
