"""Paper trading package: venue data in, optimizers decide, one ledger records.

Exports are lazy so a pure module (``books``, ``ledger``, ``latency``) can be
imported and tested without constructing the engine or touching the network.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ActionBook": "latency",
    "BookFeed": "venue",
    "Ledger": "ledger",
    "Market": "venue",
    "PaperEngine": "engine",
    "Print": "venue",
    "RttProbe": "latency",
    "best_ask": "books",
    "best_bid": "books",
    "ladder": "books",
    "mid": "books",
    "parse_market": "venue",
    "qualifying_score": "books",
    "session": "venue",
    "tape": "venue",
    "universe": "venue",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
