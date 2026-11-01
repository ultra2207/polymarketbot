"""Deterministic market categoriser driven by configs/categories.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path("configs/categories.yaml")


@lru_cache(maxsize=1)
def load_rules(path: str | Path = CONFIG) -> list[tuple[str, list[str]]]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [(r["category"], list(r["patterns"])) for r in doc["rules"]]


def sql_case(column_sql: str, path: str | Path = CONFIG) -> str:
    """Build a DuckDB CASE expression that maps text -> category.

    Rules are ordered; the first match wins. Patterns within a rule are OR-ed.
    """
    branches = []
    for category, patterns in load_rules(path):
        if category == "other":
            continue
        cond = " OR ".join(f"regexp_matches({column_sql}, {_lit(p)})" for p in patterns)
        branches.append(f"WHEN {cond} THEN {_lit(category)}")
    return "CASE " + " ".join(branches) + " ELSE 'other' END"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def classify(text: str, path: str | Path = CONFIG) -> str:
    """Python-side classifier, used for spot checks and tests."""
    import re

    txt = text.lower()
    for category, patterns in load_rules(path):
        for p in patterns:
            if re.search(p, txt):
                return category
    return "other"
