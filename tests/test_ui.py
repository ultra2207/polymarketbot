"""Unit test for Streamlit UI module existence and imports."""

from pathlib import Path


def test_ui_files_exist():
    app_path = Path("src/polymarketbot/ui/app.py")
    launcher_path = Path("scripts/run_ui.py")
    assert app_path.exists()
    assert launcher_path.exists()


def test_ui_syntax():
    import py_compile
    # Verify both compile cleanly
    py_compile.compile("src/polymarketbot/ui/app.py", doraise=True)
    py_compile.compile("scripts/run_ui.py", doraise=True)
