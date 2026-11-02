#!/usr/bin/env python3
"""Launcher for PolySimulator Streamlit Paper Trading Dashboard.

Usage:
    uv run python scripts/run_ui.py
    # Or directly via streamlit:
    uv run streamlit run src/polymarketbot/ui/app.py
"""

import sys
from pathlib import Path
import streamlit.web.cli as stcli

def main() -> None:
    app_path = Path(__file__).resolve().parent.parent / "src" / "polymarketbot" / "ui" / "app.py"
    sys.argv = ["streamlit", "run", str(app_path), "--server.port=8501", "--server.headless=true"]
    stcli.main()

if __name__ == "__main__":
    main()
