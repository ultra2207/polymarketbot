"""Phased supervision of the live reward market-making runner.

``polymarketbot.live.runner`` is the entry point and the façade: it parses the
flags, holds the live gate, binds the collaborators and calls these phases in
the original order. Each module here owns one seam of a single supervision run:

``context``    the mutable run record and the collaborators the façade binds
``helpers``    the thread bridges every phase shares
``markets``    operator-facing market helpers (open-order names, L2 score)
``desk``       live settlement buys on this venue
``startup``    venue clients, the settlement gate, wallet, market selection,
               preflight and session recording
``sizing``     the sizing cap and the quote sizing it feeds (never cash)
``feed``       WebSocket plumbing, book seeding, the do-not-quote-before-WS gate
``switch``     the rewards re-scan and the market migration it drives
``lifecycle``  market status, redemption, the halted-market settlement pass
``loop``       the tick loop and the proven-cancel shutdown

Nothing here changes behaviour: it is the single-coroutine runner split at its
labelled seams, with the same calls in the same order, the same arguments, the
same log text and the same error handling.
"""
