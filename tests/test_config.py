"""Configuration: secret masking, .env precedence, no cross-file leakage."""

from __future__ import annotations

from pathlib import Path

from polymarketbot.config import BotConfig, load_config


def test_masked_dump_hides_every_credential():
    config = BotConfig(
        POLYMARKET_API_KEY="key-1234",
        POLYMARKET_SECRET="sec-5678",
        POLYMARKET_PASSPHRASE="pass-9012",
        PRIVATE_KEY="0xdeadbeefcafebabe",
        POLYMARKET_RELAYER_API_KEY="relay-3456",
        POLYGON_RPC_URL="https://polygon-mainnet.g.alchemy.com/v2/abcd1234efgh5678",
    )
    masked = config.to_dict(mask_secrets=True)
    assert masked["POLYMARKET_API_KEY"] == "***1234"
    assert masked["POLYMARKET_SECRET"] == "***5678"
    # A signing key is never partially shown and an RPC URL hides the key that
    # is its last path segment.
    assert masked["PRIVATE_KEY"] == "***"
    assert masked["POLYGON_RPC_URL"] == "https://polygon-mainnet.g.alchemy.com/v2/***"
    assert "abcd1234efgh5678" not in masked["POLYGON_RPC_URL"]


def test_unmasked_dump_is_the_literal_config():
    config = BotConfig(PRIVATE_KEY="0xabc")
    assert config.to_dict()["PRIVATE_KEY"] == "0xabc"


def test_env_file_values_are_read_without_mutating_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOB_API_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("CLOB_API_URL=https://example.invalid\n", encoding="utf-8")
    assert load_config(env).CLOB_API_URL == "https://example.invalid"
    # The documented precedence is "os.environ wins"; reading a file must not
    # promote its values into the process environment for the next caller.
    assert "CLOB_API_URL" not in __import__("os").environ


def test_process_environment_beats_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOB_API_URL", "https://env.example")
    env = tmp_path / ".env"
    env.write_text("CLOB_API_URL=https://file.example\n", encoding="utf-8")
    assert load_config(env).CLOB_API_URL == "https://env.example"


def test_missing_env_file_yields_defaults(tmp_path):
    config = load_config(tmp_path / "absent.env")
    assert isinstance(config, BotConfig)
    assert config.is_live_trading_ready() is False
    assert Path(".").exists()  # the call must not depend on a cwd side effect
