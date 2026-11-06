"""The venue's two-sided normalisation, which decides every pull-vs-quote test.

Q_norm = Q_two + Q_min/3, so a fully one-sided quote earns a third of its raw
score. Summing the two sides additively (the old behaviour) overstates the
reward of a pulled side by 3x and therefore makes every defensive idea look
worse than it is.
"""

from __future__ import annotations

from polymarketbot.paper_trading.rewards import normalise_two_sided


def test_two_sided_quote_scores_the_sum() -> None:
    assert normalise_two_sided(10.0, 6.0) == 16.0


def test_one_sided_quote_is_penalised_by_three() -> None:
    assert normalise_two_sided(9.0, 0.0) == 3.0
    assert normalise_two_sided(0.0, 9.0) == 3.0


def test_missing_scores_are_zero_not_negative() -> None:
    assert normalise_two_sided(-5.0, 0.0) == 0.0
    assert normalise_two_sided(0.0, 0.0) == 0.0
