"""Regression tests for slug-based categorisation.

Every example here is a real slug family from the tape. If a rule edit changes
one of these, it should be a deliberate decision, not an accident.
"""

from __future__ import annotations

import re

import pytest

from polymarketbot.universe.categories import classify, load_rules


@pytest.mark.parametrize("text,expected", [
    # sports: league matchup encoding
    ("nba-dal-tor-2026-03-08-spread-home-21pt5", "sports"),
    ("atp-svrcina-faurel-2026-05-22-first-set-total-8pt5", "sports"),
    ("cs2-inf1-innerc2-2026-08-05-game3-round-total-21pt5", "sports"),
    ("fifwc-sen-irq-2026-06-26-player-props", "sports"),
    ("2026-womens-wimbledon-winner", "sports"),
    # crypto
    ("btc-updown-5m-1775124000", "crypto"),
    ("will-the-price-of-solana-be-between-100-110-on-december-2", "crypto"),
    ("what-price-will-ethereum-hit-february-2-8", "crypto"),
    # weather
    ("highest-temperature-in-london-on-march-11-2026-8c", "weather"),
    # geopolitics
    ("will-the-iranian-regime-fall-by-june-30", "geopolitics"),
    # finance vs economics: different fee rates (0.04 vs 0.05)
    ("spy-closes-above-750-on-june-16-2026", "finance"),
    ("ndx-close-below-23500-dec-2026", "finance"),
    ("will-amzn-hit-300-by-december", "finance"),
    # economics
    ("50-copper-tariff-in-effect-by-august-1", "economics"),
    ("will-the-fed-cut-rates-in-march", "economics"),
    # tech
    ("will-openai-release-gpt-6-in-2026", "tech"),
    ("when-will-spacex-launch-starship", "tech"),
    # politics
    ("will-trump-attend-jimmy-carters-state-funeral", "politics"),
    ("will-delia-velculescu-be-the-next-prime-minister-of-romania", "politics"),
    # culture
    ("what-will-be-said-during-the-seventh-episode-of-house-of-the-dragon", "culture"),
    # health
    ("measles-outbreak-in-2026", "health"),
    # genuinely unclassifiable
    ("will-some-random-thing-happen", "other"),
])
def test_classify(text, expected):
    assert classify(text) == expected


def test_sports_wins_over_crypto_when_both_present():
    # Ordering matters: "nba" must beat "bitcoin" in a compound slug.
    assert classify("nba-bitcoin-halving-game") == "sports"


def test_every_pattern_compiles():
    for category, patterns in load_rules():
        for p in patterns:
            re.compile(p)  # raises re.error on a bad rule


def test_other_is_last_and_catch_all():
    rules = load_rules()
    assert rules[-1][0] == "other"
    assert rules[-1][1] == [".*"]
