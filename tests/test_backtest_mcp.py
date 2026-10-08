"""Tests for disabling the terminal's built-in MCP server before a backtest.

MT5 build 6090+ starts its own MCP server (the transport behind the built-in AI
assistant) at launch, configured in ``<terminal>/Config/assistant.ini`` and
defaulting to 127.0.0.1:22346. Every terminal in a VM shares loopback, so only
one can hold the port and the rest log ``MCP bind error on 127.0.0.1:22346
[10048]`` on every launch; the same subsystem also authenticates against
MQL5.community, which a backtest terminal has no account for. The MCP server is
an AI-agent feature with no role in backtesting, so the handler disables it
before each run. These tests pin that behaviour and the encoding MT5 expects.
"""
from __future__ import annotations

import configparser
import os

from mt5api.backtest import handler
from mt5api.config import terminal_mcp_disabled


def test_mcp_disable_is_opt_in():
    """An absent block leaves MT5 as shipped — the feature must never turn
    itself on for an install that upgraded without asking."""
    assert terminal_mcp_disabled("backtest", {}, None) is False
    assert terminal_mcp_disabled("backtest", None, None) is False
    assert terminal_mcp_disabled("backtest", {"disable_terminal_server": False}, None) is False
    assert terminal_mcp_disabled("backtest", {"disable_terminal_server": True}, None) is True


def test_mcp_disable_is_backtest_only():
    assert terminal_mcp_disabled("live", {"disable_terminal_server": True}, None) is False


def test_a_terminals_own_mcp_false_opts_it_out():
    cfg = {"disable_terminal_server": True}
    assert terminal_mcp_disabled("backtest", cfg, {"mcp": False}) is False
    assert terminal_mcp_disabled("backtest", cfg, {"mcp": True}) is True
    assert terminal_mcp_disabled("backtest", cfg, {}) is True


def test_mcp_disable_tolerates_a_malformed_block():
    assert terminal_mcp_disabled("backtest", "yes", None) is False
    assert terminal_mcp_disabled("backtest", {"disable_terminal_server": True}, "nope") is True


def _read_assistant_ini(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    assert raw[:2] == b"\xff\xfe", "MT5 assistant.ini must be UTF-16-LE with a BOM"
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read_string(raw.decode("utf-16-le").lstrip("\ufeff"))
    return parser


def _terminal_dir(monkeypatch, tmp_path):
    terminal_dir = tmp_path / "terminal"
    (terminal_dir / "Config").mkdir(parents=True)
    monkeypatch.setattr(handler, "TERMINAL_DIR", str(terminal_dir))
    return terminal_dir


def test_creates_a_disabled_file_when_absent(monkeypatch, tmp_path):
    terminal_dir = _terminal_dir(monkeypatch, tmp_path)
    path = os.path.join(str(terminal_dir), "Config", "assistant.ini")
    assert not os.path.exists(path)

    handler._disable_terminal_mcp()

    parser = _read_assistant_ini(path)
    assert parser["MCP.MetaTrader"]["Enable"] == "0"
    assert parser["MCP.MetaEditor"]["Enable"] == "0"


def test_preserves_endpoint_and_api_key_while_disabling(monkeypatch, tmp_path):
    terminal_dir = _terminal_dir(monkeypatch, tmp_path)
    path = os.path.join(str(terminal_dir), "Config", "assistant.ini")
    original = (
        "[MCP.MetaEditor]\r\n"
        "Enable=1\r\n"
        "Endpoint=http://127.0.0.1:22345/mcp\r\n"
        "ApiKey=editor-key\r\n"
        "[MCP.MetaTrader]\r\n"
        "Enable=1\r\n"
        "Endpoint=http://127.0.0.1:22346/mcp\r\n"
        "ApiKey=terminal-key\r\n"
        "[MCP.Custom]\r\n"
        "Keep=me\r\n"
    )
    with open(path, "wb") as handle:
        handle.write(b"\xff\xfe")
        handle.write(original.encode("utf-16-le"))

    handler._disable_terminal_mcp()

    parser = _read_assistant_ini(path)
    assert parser["MCP.MetaTrader"]["Enable"] == "0"
    assert parser["MCP.MetaEditor"]["Enable"] == "0"
    # The endpoint and key are not the point of the change; only Enable flips.
    assert parser["MCP.MetaTrader"]["Endpoint"] == "http://127.0.0.1:22346/mcp"
    assert parser["MCP.MetaTrader"]["ApiKey"] == "terminal-key"
    assert parser["MCP.Custom"]["Keep"] == "me"


def test_a_corrupt_file_is_replaced_not_fatal(monkeypatch, tmp_path):
    terminal_dir = _terminal_dir(monkeypatch, tmp_path)
    path = os.path.join(str(terminal_dir), "Config", "assistant.ini")
    with open(path, "wb") as handle:
        handle.write(b"\xff\xfe")
        handle.write("this is not an ini\u0000garbage".encode("utf-16-le"))

    handler._disable_terminal_mcp()

    parser = _read_assistant_ini(path)
    assert parser["MCP.MetaTrader"]["Enable"] == "0"
    assert parser["MCP.MetaEditor"]["Enable"] == "0"


def test_reapplied_on_every_run(monkeypatch, tmp_path):
    """MT5 rewrites assistant.ini on exit, so a second call must re-disable."""
    terminal_dir = _terminal_dir(monkeypatch, tmp_path)
    path = os.path.join(str(terminal_dir), "Config", "assistant.ini")
    handler._disable_terminal_mcp()
    # Simulate MT5 rewriting the file with the server enabled again.
    with open(path, "wb") as handle:
        handle.write(b"\xff\xfe")
        handle.write("[MCP.MetaTrader]\r\nEnable=1\r\n".encode("utf-16-le"))

    handler._disable_terminal_mcp()

    parser = _read_assistant_ini(path)
    assert parser["MCP.MetaTrader"]["Enable"] == "0"


def test_a_write_failure_is_not_fatal(monkeypatch, tmp_path):
    """Disabling MCP is a mitigation; a terminal with it enabled can still run,
    so an unwritable assistant.ini must not fail the backtest."""
    _terminal_dir(monkeypatch, tmp_path)

    def boom(_parser, _path):
        raise OSError("disk full")

    monkeypatch.setattr(handler, "_write_utf16_ini", boom)
    handler._disable_terminal_mcp()  # must not raise

