"""A crossing delta must never reach the book, because a crossed book publishes
``spread == 0`` and the reward scorer turns that into ``alpha = ((v-s)/v)^2 = 1``
- a maximum score share on a book that does not exist.

Reproduced before the guard: a ``price_change`` SELL delta priced below the best
bid became the new best ask (asks 0.02 / 0.58 / 0.60 -> best_ask 0.02,
spread 0.0, mid 0.42), so every consumer that prices off the mid was reading a
book the venue would never show.
"""

from __future__ import annotations

import pytest

from polymarketbot.strategy.venue.feed.book import _TokenBook


def _book() -> _TokenBook:
    book = _TokenBook()
    assert book.apply_change("bid", 0.56, 100.0) is True
    assert book.apply_change("ask", 0.58, 100.0) is True
    return book


def test_an_ask_below_the_best_bid_is_rejected() -> None:
    book = _book()

    assert book.apply_change("ask", 0.02, 50.0) is False

    snapshot = book.to_snapshot("tok")
    assert snapshot.best_ask == pytest.approx(0.58)
    assert snapshot.best_bid == pytest.approx(0.56)
    assert snapshot.spread > 0.0
    assert snapshot.spread_cents > 0.0


def test_a_bid_above_the_best_ask_is_rejected() -> None:
    book = _book()

    assert book.apply_change("bid", 0.99, 50.0) is False

    snapshot = book.to_snapshot("tok")
    assert snapshot.best_bid == pytest.approx(0.56)
    assert snapshot.best_ask == pytest.approx(0.58)
    assert snapshot.spread > 0.0


def test_a_size_zero_delta_still_removes_a_level() -> None:
    """Rejection applies only to crossing *additions*; removals stay allowed."""
    book = _book()

    assert book.apply_change("ask", 0.58, 0.0) is True
    assert book.to_snapshot("tok").best_ask == pytest.approx(1.0)


def test_a_legal_widening_delta_is_accepted() -> None:
    book = _book()

    assert book.apply_change("ask", 0.62, 25.0) is True
    snapshot = book.to_snapshot("tok")
    assert snapshot.best_ask == pytest.approx(0.58)  # 0.58 is still the best ask
    assert snapshot.spread > 0.0
