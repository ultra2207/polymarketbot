"""Real-Money Polymarket Trading Package.

Provides production-grade live CLOB order management, EIP-712 order signing,
on-chain Polygon CTF settlement, shared market-making core architecture,
and the real-money trading engine.

This package deliberately re-exports nothing.  Import each symbol from the
module that defines it (``polymarketbot.trading.engine``,
``polymarketbot.trading.client``, ...): a package-level re-export would make
``import polymarketbot.trading.<leaf>`` execute the whole live stack, dragging
the trading engine, the live CLOB client and the OR-Tools allocator into
processes -- such as the paper trader -- that never use them.
"""
