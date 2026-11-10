"""Derived-data build pipeline.

Each module is a runnable stage (``python -m polymarketbot.data.builders.<name>``)
that reads ``data/raw`` and writes ``data/processed``. Stages are ordered: labels
before categories/statistics, those before the point-in-time universe.
"""
