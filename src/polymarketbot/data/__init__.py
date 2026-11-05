"""Institutional data collection package.

Streams live in ``polymarketbot.data.streams`` and are discovered at runtime.
Each stream module exposes ``NAME`` and ``async def run(ctx)``.
"""

from polymarketbot.data.config import load_config
from polymarketbot.data.context import CollectorContext, HealthRegistry
from polymarketbot.data.schema import SCHEMAS
from polymarketbot.data.writer import WriterHub

__all__ = ["CollectorContext", "HealthRegistry", "SCHEMAS", "WriterHub", "load_config"]
