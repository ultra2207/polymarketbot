"""Domain 2: UMA settlement basis and resolution-risk models.

Provides:
- ``types``: shared data structures for proposals, disputes and resolutions.
- ``robust_hazard``: dispute hazard, liveness and gas-delay modelling.
- ``settlement_fair_value``: oracle-verdict fair value and decay.
- ``tail_evidence``: measured tail-risk evidence loading.
- ``settlement_allocator``: marginal settlement allocation.

This package deliberately re-exports nothing.  Import each symbol from the
module that defines it (``polymarketbot.trading.domain2.types``,
``polymarketbot.trading.domain2.settlement_allocator``, ...) so that a consumer
of a single submodule executes only that submodule.
"""
