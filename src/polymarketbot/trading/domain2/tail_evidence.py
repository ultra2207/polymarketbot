"""Measured evidence for the settlement tail: how often a verdict is wrong.

The settlement harvester buys the token a *live proposal* claims will pay 1.00
and holds it to redemption.  Its loss branch is therefore

    "the final payout differs from the verdict the proposal asserted"

which is **not** the dispute hazard.  A wrong proposal that nobody disputes
finalises wrongly and the dispute hazard says nothing about it; conversely a
disputed proposal can be upheld by the DVM and still pay par.  The two risks are
different events and only the first one loses money.

The quantity this module serves is measured by
``data/derived/uma/outcome_accuracy.py``: the tape's proposals joined to Gamma's
final payout by the question text, plus the venue-scale challenge rate from
Gamma's own resolution sequence.  The artifact is

    data/derived/uma/outcome_accuracy.json

An exact on-chain join was attempted first and is not available with the
contracts this repo watches: ``data/derived/uma/chain_outcome_accuracy.py``
walks 10,000-block windows for ``ProposePrice`` and the adapters' resolve events
and found 136 proposals but **zero** adapter resolves in the same 1.5 days, so
the CTF adapter that settles Polymarket's questions is not one of the three
configured addresses.  The text join plus the challenge-rate bound is what the
evidence therefore rests on, and both are measured over populations far larger
than the handful of proposals whose final payout can be joined.

and this module refuses - ``UnmeasuredTail`` - when it is missing or when it
holds no trials.  An unmeasured tail is never replaced by a number.

Model-risk haircut: the reversal is rare and often *unobserved*, so the sizing
uses an upper credible bound of its rate, not the point estimate.  A zero-event
sample still bounds the rate; a sample of one event does not pin it, and the
bound widens honestly instead of the code pretending to know.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from polymarketbot.logs import get_logger

log = get_logger("domain2_tail_evidence")

# Written by ``data/derived/uma/outcome_accuracy.py``.
OUTCOME_ACCURACY_PATH = "data/derived/uma/outcome_accuracy.json"
# Jeffreys prior on a Bernoulli rate: Beta(1/2, 1/2).  A documented statistical
# choice (the reference non-informative prior for a binomial proportion), not an
# economic parameter.
JEFFREYS_ALPHA = 0.5
JEFFREYS_BETA = 0.5


class UnmeasuredTail(RuntimeError):
    """The outcome-error tail has no measurement; sizing must not invent one."""


@dataclass(frozen=True)
class TailEvidence:
    """Two measured channels for the settlement harvester's loss branch.

    ``trials`` / ``reversals`` are the **direct** measurement: proposals whose
    final payout is observable, and the ones where that payout contradicts the
    verdict the proposal asserted.  It is the right quantity and the wrong
    instrument - the event is rare, so a small sample bounds it loosely (0 of 70
    leaves a 2.7 % upper bound).

    ``challenge_trials`` / ``challenges`` are the **mechanistic** channel, at
    venue scale: every closed market whose UMA resolution status shows a
    dispute.  A reversal cannot happen without a challenge, so

        P(reversal) <= P(challenged)

    holds by construction, and the challenge rate is measured over the whole
    closed population instead of over the few proposals whose final payout could
    be joined.  The residual it does not cover is an admin/emergency resolution
    that flips an undisputed proposal without a dispute entry; that channel is
    not observable in the fetched population, so the direct sample's bound is
    reported next to it rather than discarded.

    Proposals with an unobservable final payout are excluded from ``trials`` and
    from ``reversals``: counting them as agreement would manufacture a zero.
    """

    trials: int
    reversals: int
    disputed: int
    source: str
    proposals_seen: int = 0
    challenge_trials: int = 0
    challenges: int = 0

    def __post_init__(self) -> None:
        if self.trials < 0 or self.reversals < 0 or self.challenges < 0:
            raise ValueError("tail evidence counts cannot be negative")
        if self.reversals > self.trials:
            raise ValueError(
                f"tail evidence has {self.reversals} reversals over {self.trials} trials"
            )
        if self.challenges > self.challenge_trials and self.challenge_trials:
            raise ValueError(
                f"tail evidence has {self.challenges} challenges over "
                f"{self.challenge_trials} markets"
            )

    @property
    def q_point(self) -> float:
        """Point estimate of the reversal rate (``0`` is a real measurement)."""
        return self.reversals / self.trials if self.trials else float("nan")

    @staticmethod
    def _upper(events: int, trials: int, level: float) -> float:
        if not 0.0 < level < 1.0:
            raise ValueError(f"credible level must be in (0, 1), got {level!r}")
        if trials <= 0:
            raise UnmeasuredTail("no observations: the tail is unmeasured")
        from scipy.stats import beta as beta_dist

        return float(
            beta_dist.ppf(
                level,
                events + JEFFREYS_ALPHA,
                trials - events + JEFFREYS_BETA,
            )
        )

    def q_direct_upper(self, level: float) -> float:
        """Upper ``level`` bound of the *observed* reversal rate."""
        return self._upper(self.reversals, self.trials, level)

    def q_challenge_upper(self, level: float) -> float:
        """Upper ``level`` bound of the challenge rate, hence of reversal."""
        if self.challenge_trials <= 0:
            raise UnmeasuredTail(
                "no closed-market population was measured for the challenge channel"
            )
        return self._upper(self.challenges, self.challenge_trials, level)

    def q_loss_upper(self, level: float) -> float:
        """The loss-branch probability the sizing uses.

        The mechanistic bound when the challenge population was measured, and
        the direct bound when it was not - never a number the evidence does not
        support.  ``P(reversal) <= P(challenged)`` is why the former is a valid
        stand-in for the latter; :meth:`describe` prints both so the residual is
        visible on every allocation line.
        """
        try:
            return self.q_challenge_upper(level)
        except UnmeasuredTail:
            return self.q_direct_upper(level)

    def scaled(self, factor: float) -> "TailEvidence":
        """The same evidence with both event counts multiplied by ``factor``.

        Used for the sensitivity statement the sizing has to carry: a tail that
        is twice as large must change the size, and must not change the sign of
        the decision.
        """
        if factor <= 0.0:
            raise ValueError(f"sensitivity factor must be positive, got {factor!r}")
        return replace(
            self,
            reversals=int(round(self.reversals * factor)),
            challenges=int(round(self.challenges * factor)),
            source=f"{self.source} x{factor:g}",
        )

    def describe(self) -> str:
        parts = [
            f"direct {self.reversals}/{self.trials} proposal verdicts reversed "
            f"(q={self.q_point:.6f}, {self.disputed} disputed)"
        ]
        if self.challenge_trials > 0:
            parts.append(
                f"challenged {self.challenges}/{self.challenge_trials} closed markets "
                f"({self.challenges / self.challenge_trials:.6f}); a reversal needs a "
                f"challenge, so that rate bounds it"
            )
        parts.append(f"source {self.source}")
        return "; ".join(parts)


def load_tail_evidence(path: str = OUTCOME_ACCURACY_PATH) -> TailEvidence:
    """Read the measured tail; raise ``UnmeasuredTail`` when there is none."""
    p = Path(path)
    if not p.exists():
        raise UnmeasuredTail(
            f"no measured outcome-error tail at {path}: run "
            "`uv run python data/derived/uma/outcome_accuracy.py`"
        )
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnmeasuredTail(f"outcome-error artifact at {path} is unreadable: {exc}") from exc
    trials = int(payload.get("trials") or 0)
    if trials <= 0:
        raise UnmeasuredTail(
            f"outcome-error artifact at {path} holds no observable proposal finals"
        )
    return TailEvidence(
        trials=trials,
        reversals=int(payload.get("reversals") or 0),
        disputed=int(payload.get("disputed") or 0),
        source=str(payload.get("source") or path),
        proposals_seen=int(payload.get("proposals_seen") or 0),
        challenge_trials=int(payload.get("challenge_trials") or 0),
        challenges=int(payload.get("challenges") or 0),
    )


def evidence_from_counts(
    trials: int,
    reversals: int,
    *,
    disputed: int = 0,
    source: str = "caller",
    challenge_trials: int = 0,
    challenges: int = 0,
) -> TailEvidence:
    """Construct evidence from explicit counts (tests, or a measured report)."""
    return TailEvidence(
        trials=int(trials),
        reversals=int(reversals),
        disputed=int(disputed),
        source=source,
        challenge_trials=challenge_trials,
        challenges=challenges,
    )
