"""Paper trading package: venue data in, optimizers decide, one ledger records.

Exports are lazy so the strategy core's pure modules (``books``, ``ledger``,
``latency``, ``venue.reads``) can be imported and tested without constructing
the engine or touching the network.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ActionBook": "polymarketbot.strategy.latency",
    "BookFeed": "polymarketbot.strategy.venue.reads",
    "Ledger": "polymarketbot.strategy.ledger",
    "Market": "polymarketbot.strategy.venue.reads",
    "PaperEngine": "engine",
    "Print": "polymarketbot.strategy.venue.reads",
    "RttProbe": "polymarketbot.strategy.latency",
    "best_ask": "polymarketbot.strategy.books",
    "best_bid": "polymarketbot.strategy.books",
    "ladder": "polymarketbot.strategy.books",
    "mid": "polymarketbot.strategy.books",
    "parse_market": "polymarketbot.strategy.venue.reads",
    "qualifying_score": "polymarketbot.strategy.books",
    "session": "polymarketbot.strategy.venue.reads",
    "tape": "polymarketbot.strategy.venue.reads",
    "universe": "polymarketbot.strategy.venue.reads",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    path = module_name if module_name.startswith("polymarketbot.") else f"{__name__}.{module_name}"
    value = getattr(import_module(path), name)
    globals()[name] = value
    return value
