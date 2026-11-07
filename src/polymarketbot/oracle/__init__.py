"""Polymarket Oracle Domain Subsystem.

Adapters for multi-venue price discovery, real-world event feeds (crypto, sports,
weather, politics, macro), UMA ancillary data parsing, and deterministic/probabilistic settlement evaluation.
"""

from __future__ import annotations

from polymarketbot.oracle.base import (
    BaseOracleAdapter,
    GroundTruthOutcome,
    OracleCategory,
    OracleFeedSource,
    OracleStatus,
    ParsedAncillaryData,
    PricePoint,
    ResolutionOutcome,
    ResolutionVerdict,
)
from polymarketbot.oracle.crypto import CryptoOracleAdapter
from polymarketbot.oracle.sports import SportsOracleAdapter
from polymarketbot.oracle.macro import MacroOracleAdapter
from polymarketbot.oracle.weather import WeatherOracleAdapter
from polymarketbot.oracle.politics import PoliticsOracleAdapter
from polymarketbot.oracle.pipeline import ExogenousOraclePipeline, create_default_oracle_pipeline

__all__ = [
    "BaseOracleAdapter",
    "GroundTruthOutcome",
    "OracleCategory",
    "OracleFeedSource",
    "OracleStatus",
    "ParsedAncillaryData",
    "PricePoint",
    "ResolutionOutcome",
    "ResolutionVerdict",
    "CryptoOracleAdapter",
    "SportsOracleAdapter",
    "MacroOracleAdapter",
    "WeatherOracleAdapter",
    "PoliticsOracleAdapter",
    "ExogenousOraclePipeline",
    "create_default_oracle_pipeline",
]
