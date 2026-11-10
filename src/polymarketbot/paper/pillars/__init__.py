"""Paper-trading pillars: the runners that wire the strategy core to the ledger.

The decision functions moved to the shared core: reward decisions live in
``polymarketbot.strategy.reward.decision``.  These submodules keep the
paper-only bookkeeping (fills, rebates) and are imported explicitly so one
runner can be tested without importing the others.
"""
