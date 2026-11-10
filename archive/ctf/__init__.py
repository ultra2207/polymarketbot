"""Archived CTF complete-set strategy (mint/merge parity and complement quoting).

The strategy was archived on measured data, not on taste: see ``README.md`` in
this directory for the census numbers and the re-run commands.  Nothing here is
imported by ``polymarketbot``; this package is kept so the measurement can be
repeated if the market structure changes.

Layout:

* :mod:`.pillars` - the decision package (taker parity, maker parity, measured
  fill/markout tables, on-chain leg clock).  Imported as ``archive.ctf.pillars``.
* :mod:`.runner` - the engine-side loop that funded the pillar (formerly
  ``polymarketbot.paper.pillars.ctf_runner``).
* :mod:`.allocator` - the marginal capital allocator (formerly
  ``polymarketbot.trading.ctf_allocator``).
* :mod:`.split_arbitrage` - fee/gas primitives the allocator prices with
  (formerly ``polymarketbot.trading.ctf_split_arbitrage``).
* :mod:`.complement_quoter` - the asynchronous two-sided complement quoter and
  auto-merger (formerly ``polymarketbot.trading.complement_quoter``).
* :mod:`.books` / :mod:`.harness` / :mod:`.measurements` / :mod:`.merger` /
  :mod:`.strategy_types` - small pure helpers copied out of the live packages so
  the archive has no dependency on ``polymarketbot.paper`` or the retired
  ``polymarketbot.trading`` package (the live half is now
  ``polymarketbot.live``; see README "Copied helpers").
* :mod:`.tests` - the moved tests; run them by explicit path from the
  repository root:
  ``uv run python -m pytest -q archive/ctf/tests``.
"""

from __future__ import annotations

from . import pillars

__all__ = ["pillars"]
