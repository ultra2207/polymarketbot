"""Master Exogenous Real-World Oracle Pipeline for Domain 2.

Dispatches market candidates to domain-specific adapters (Crypto, Sports, Macro,
Weather, Politics) to detect real-world conclusion events in real time.
Overrides p_hat=1.000 (or 0.000) and hazard h_reversal=0.000 to enable
instantaneous harvesting of the 5-45 minute In-Play Conclusion Gap.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Type, TYPE_CHECKING

from polymarketbot.logs import get_logger
from polymarketbot.oracle.base import (
    BaseOracleAdapter,
    GroundTruthOutcome,
    OracleCategory,
    OracleStatus,
)

if TYPE_CHECKING:
    from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate

log = get_logger("oracle_pipeline")


class ExogenousOraclePipeline:
    """Master orchestrator integrating all domain-specific oracle feeds."""

    def __init__(self, adapters: Optional[List[BaseOracleAdapter]] = None) -> None:
        self.adapters: List[BaseOracleAdapter] = list(adapters or [])
        self._concluded_cache: Dict[str, GroundTruthOutcome] = {}
        log.info(f"Initialized ExogenousOraclePipeline with {len(self.adapters)} adapters.")

    def register_adapter(self, adapter: BaseOracleAdapter) -> None:
        """Register a new domain adapter."""
        self.adapters.append(adapter)
        log.info(f"Registered oracle adapter: {adapter.name} [{adapter.category.value}]")

    def find_adapter(
        self,
        question: str,
        resolution_source: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Optional[BaseOracleAdapter]:
        """Find the most appropriate adapter capable of evaluating the question."""
        for adapter in self.adapters:
            try:
                if adapter.can_handle(question, resolution_source, category):
                    return adapter
            except Exception as e:
                log.warning(f"Error checking adapter {adapter.name}: {e}")
        return None

    def evaluate_candidate(
        self,
        candidate: Domain2Candidate,
        resolution_source: Optional[str] = None,
    ) -> Optional[GroundTruthOutcome]:
        """Evaluate a Domain2Candidate against exogenous ground truth.

        If a concluded event is identified:
        1. candidate.p_hat is set to outcome.p_hat (1.000 or 0.000).
        2. Returns the GroundTruthOutcome.
        """
        # Check cache first
        if candidate.condition_id in self._concluded_cache:
            cached = self._concluded_cache[candidate.condition_id]
            candidate.p_hat = cached.p_hat
            return cached

        adapter = self.find_adapter(
            question=candidate.question,
            resolution_source=resolution_source,
            category=candidate.category,
        )
        if not adapter:
            return None

        try:
            outcome = adapter.evaluate(candidate)
            if outcome is not None:
                if outcome.is_deterministic_conclusion:
                    log.info(
                        f"[ORACLE CONCLUSION GAP DETECTED] {candidate.condition_id[:10]}... "
                        f"Q: '{candidate.question}' -> Winner: {outcome.winning_outcome} "
                        f"p_hat={outcome.p_hat:.3f} Source: {outcome.resolution_source_url}"
                    )
                    # Cache deterministic conclusion
                    self._concluded_cache[candidate.condition_id] = outcome
                    candidate.p_hat = outcome.p_hat
                return outcome
        except Exception as e:
            log.error(f"Failed to evaluate candidate {candidate.condition_id} with adapter {adapter.name}: {e}")

        return None

    def evaluate_candidates(
        self,
        candidates: List[Domain2Candidate],
        market_metadata: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> List[Domain2Candidate]:
        """Evaluate a batch of Domain2Candidate objects, applying oracle overrides in place."""
        meta = market_metadata or {}
        concluded_count = 0

        for candidate in candidates:
            res_source = meta.get(candidate.condition_id, {}).get("resolution_source")
            outcome = self.evaluate_candidate(candidate, resolution_source=res_source)
            if outcome and outcome.is_deterministic_conclusion:
                concluded_count += 1

        if concluded_count > 0:
            log.info(f"Oracle pipeline applied ground-truth overrides to {concluded_count}/{len(candidates)} candidates.")
        return candidates

    def poll_all_sources(self) -> Dict[str, GroundTruthOutcome]:
        """Poll active feeds from all registered adapters."""
        all_outcomes: Dict[str, GroundTruthOutcome] = {}
        for adapter in self.adapters:
            try:
                outcomes = adapter.poll_active_events()
                all_outcomes.update(outcomes)
            except Exception as e:
                log.error(f"Error polling adapter {adapter.name}: {e}")
        return all_outcomes


def create_default_oracle_pipeline() -> ExogenousOraclePipeline:
    """Instantiate and configure the complete production oracle pipeline with all 5 cluster adapters."""
    from polymarketbot.oracle.crypto import CryptoOracleAdapter
    from polymarketbot.oracle.sports import SportsOracleAdapter
    from polymarketbot.oracle.macro import MacroOracleAdapter
    from polymarketbot.oracle.weather import WeatherOracleAdapter
    from polymarketbot.oracle.politics import PoliticsOracleAdapter

    pipeline = ExogenousOraclePipeline([
        CryptoOracleAdapter(),
        SportsOracleAdapter(),
        MacroOracleAdapter(),
        WeatherOracleAdapter(),
        PoliticsOracleAdapter(),
    ])
    return pipeline
