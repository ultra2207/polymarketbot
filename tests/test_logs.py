"""Logging: repo-anchored output dir, one sink set per source file."""

from __future__ import annotations

from pathlib import Path

from polymarketbot import logs
from polymarketbot.logs import get_logger


def test_log_dir_is_absolute_and_anchored_to_the_checkout():
    # A relative LOG_DIR resolved against whatever cwd the process started in,
    # which scattered log files for every tool launched from another directory.
    assert logs.LOG_DIR.is_absolute()
    assert (logs._ROOT / "pyproject.toml").exists()
    assert logs.LOG_DIR.parent == logs._ROOT / "data"


def test_repeated_get_logger_reuses_the_same_sinks():
    first = get_logger("dup-check")
    second = get_logger("dup-check")
    source = Path(__file__).resolve()
    # Two bindings, one sink set: importing a module twice must not double
    # every line it writes.
    assert len(logs._SINKS[str(source)]) == len(set(logs._SINKS[str(source)]))
    assert first is not second  # separate bindings share the sinks


def test_records_are_stamped_in_ist_with_a_name():
    from loguru import logger

    captured: list[str] = []
    sink = logger.add(lambda message: captured.append(message), format="{extra[name]}")
    try:
        get_logger("stamp-check").info("hello")
    finally:
        logger.remove(sink)
    assert "stamp-check" in "".join(captured)


# ------------------------------------------------------------------ rotation
def _sink(tmp_path, **kwargs) -> logs._RotatingFileSink:
    sink = logs._RotatingFileSink(tmp_path / "app.log", **kwargs)
    sink._ensure_open()
    return sink


def test_rotation_splits_the_file_at_the_threshold(tmp_path):
    sink = _sink(tmp_path, rotation="1 KB", retention=2, compression=None)
    sink.write("a" * 600 + "\n")
    sink.write("b" * 600 + "\n")  # crosses 1 KB -> rotate first
    sink.stop()

    live = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert live.startswith("b")
    rotated = [p for p in tmp_path.iterdir() if p.name != "app.log"]
    assert len(rotated) == 1
    assert rotated[0].read_text(encoding="utf-8").startswith("a")


def test_a_blocked_rotation_never_loses_a_record(tmp_path, monkeypatch, capsys):
    """Two processes hold app.log on Windows, so the rename raises PermissionError."""
    sink = _sink(tmp_path, rotation="1 KB", retention=2, compression=None)

    def refused(*_args, **_kwargs):
        raise PermissionError("held by another process")

    monkeypatch.setattr("polymarketbot.logs.os.replace", refused)
    sink.write("x" * 600 + "\n")
    sink.write("y" * 600 + "\n")
    monkeypatch.undo()
    sink.write("z" * 10 + "\n")
    sink.stop()

    # Every line survives somewhere: while the rename was refused the file grew
    # past the threshold; the first rename that succeeds moves those lines into a
    # rotated part, so the record set is the union of the directory.
    text = "".join(p.read_text(encoding="utf-8") for p in sorted(tmp_path.iterdir()))
    assert text.count("\n") == 3
    assert text.startswith("x") and text.endswith("z" * 10 + "\n")
    err = capsys.readouterr().err
    assert err.count("cannot be rotated") == 1  # said once, not per record


def test_retention_keeps_the_newest_rotated_files(tmp_path):
    sink = _sink(tmp_path, rotation="100 B", retention=1, compression=None)
    for i in range(4):
        sink.write(f"{i}" * 60 + "\n")
        sink.write(f"{i}" * 60 + "\n")
    sink.stop()
    rotated = [p for p in tmp_path.iterdir() if p.name != "app.log"]
    assert len(rotated) == 1


def test_gz_compression_replaces_the_plain_rotated_file(tmp_path):
    import gzip

    sink = _sink(tmp_path, rotation="100 B", retention=2, compression="gz")
    sink.write("a" * 120 + "\n")
    sink.write("b" * 120 + "\n")
    sink.stop()
    compressed = list(tmp_path.glob("app.*.log.gz"))
    assert len(compressed) == 1
    assert not list(tmp_path.glob("app.*.log"))
    with gzip.open(compressed[0], "rt", encoding="utf-8") as fh:
        assert fh.read().startswith("a")


def test_rotation_spec_parsing():
    assert logs._parse_size("10 MB") == 10 * 1024 ** 2
    assert logs._parse_size("512kb") == 512 * 1024
    assert logs._parse_size("2048") == 2048
    assert logs._parse_size("00:00") is None
    assert logs._parse_size("nonsense") is None
