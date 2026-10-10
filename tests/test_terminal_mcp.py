"""Turning off MetaTrader's own MCP servers before a terminal launches.

The failure: build 6090+ terminals start an MCP server on 127.0.0.1:22346 from
Config/assistant.ini, with ``Enable=1`` as shipped. With several terminals in a
VM only the first binds the port, and every other one logs ``MCP bind error on
127.0.0.1:22346 (10048)`` on each launch. ``SHIPPED`` below is the file such a
terminal writes. These tests pin that the file is turned off right before every
kind of launch (boot, API restart, backtest) when configured, and left alone
when not. Based on the tests in #29 by @Marinski.
"""
from __future__ import annotations

import configparser
import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import mt5api.mt5client as mc
from mt5api import terminal_mcp
from mt5api.backtest import handler
from tests.test_backtest_relaunch import _submit, client  # noqa: F401 - fixture

SHIPPED = (
    "[MCP.MetaEditor]\r\n"
    "Enable=1\r\n"
    "Endpoint=http://127.0.0.1:22345/mcp\r\n"
    "ApiKey=editor-key\r\n"
    "[MCP.MetaTrader]\r\n"
    "Enable=1\r\n"
    "Endpoint=http://127.0.0.1:22346/mcp\r\n"
    "ApiKey=terminal-key\r\n"
    "[MCP.Custom]\r\n"
)
ON = {section: "1" for section in terminal_mcp.SECTIONS}
OFF = {section: "0" for section in terminal_mcp.SECTIONS}


def _write_shipped(terminal_dir) -> Path:
    path = Path(terminal_dir) / terminal_mcp.ASSISTANT_INI
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe" + SHIPPED.encode("utf-16-le"))
    return path


def _read(path) -> configparser.RawConfigParser:
    raw = Path(path).read_bytes()
    assert raw[:2] == b"\xff\xfe", "MT5 reads assistant.ini as UTF-16-LE with a BOM"
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read_string(raw.decode("utf-16-le").lstrip("\ufeff"))
    return parser


def _enabled(path) -> dict:
    parser = _read(path)
    return {section: parser[section]["Enable"] for section in terminal_mcp.SECTIONS}


# -- which terminals ----------------------------------------------------------

@pytest.mark.parametrize(
    ("global_value", "terminal_entry", "expected"),
    [
        (None, None, False),
        (False, None, False),
        ("yes", None, False),
        (True, None, True),
        (True, {}, True),
        (True, {"disable_terminal_mcp": True}, True),
        (True, {"disable_terminal_mcp": False}, False),
        (True, "not-a-mapping", True),
    ],
)
def test_only_an_explicit_true_turns_it_on_and_a_terminal_can_opt_out(
    global_value, terminal_entry, expected,
):
    assert terminal_mcp.wanted(global_value, terminal_entry) is expected


# -- the file -----------------------------------------------------------------

def test_the_shipped_file_is_turned_off_and_everything_else_kept(tmp_path):
    path = _write_shipped(tmp_path)
    assert _enabled(path) == ON

    terminal_mcp.disable(str(tmp_path))

    parser = _read(path)
    assert _enabled(path) == OFF
    assert parser["MCP.MetaTrader"]["Endpoint"] == "http://127.0.0.1:22346/mcp"
    assert parser["MCP.MetaTrader"]["ApiKey"] == "terminal-key"
    assert parser["MCP.MetaEditor"]["ApiKey"] == "editor-key"
    assert parser.has_section("MCP.Custom")


def test_a_missing_file_is_created_turned_off(tmp_path):
    path = terminal_mcp.disable(str(tmp_path))

    assert _enabled(path) == OFF


def test_a_corrupt_file_is_replaced_turned_off(tmp_path):
    path = Path(tmp_path) / terminal_mcp.ASSISTANT_INI
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe" + "not an ini\u0000garbage".encode("utf-16-le"))

    terminal_mcp.disable(str(tmp_path))

    assert _enabled(path) == OFF


def test_a_file_mt5_rewrote_on_exit_is_turned_off_again(tmp_path):
    terminal_mcp.disable(str(tmp_path))
    path = _write_shipped(tmp_path)

    terminal_mcp.disable(str(tmp_path))

    assert _enabled(path) == OFF


def test_an_unwritable_file_is_logged_and_never_blocks_the_launch(tmp_path, monkeypatch, caplog):
    def _refuse(_terminal_dir):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(terminal_mcp, "disable", _refuse)

    terminal_mcp.apply(True, str(tmp_path), "restart")

    assert "terminal MCP servers left on before restart launch" in caplog.text


def test_apply_leaves_the_file_alone_when_not_configured(tmp_path):
    path = _write_shipped(tmp_path)

    terminal_mcp.apply(False, str(tmp_path), "restart")

    assert _enabled(path) == ON


# -- every launch path --------------------------------------------------------

def _load_config_helper():
    path = Path(__file__).resolve().parent.parent / "scripts" / "config_helper.py"
    spec = importlib.util.spec_from_file_location("config_helper_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("config_lines", "terminal_lines", "expected"),
    [
        ("disable_terminal_mcp: true\n", "", OFF),
        ("", "", ON),
        ("disable_terminal_mcp: true\n", "    disable_terminal_mcp: false\n", ON),
    ],
)
def test_boot_turns_it_off_before_the_terminal_starts(
    tmp_path, monkeypatch, config_lines, terminal_lines, expected,
):
    helper = _load_config_helper()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        config_lines
        + "accounts:\n  b:\n    a:\n      login: 1\n      server: S\n      password: p\n"
        + "terminals:\n  - broker: b\n    account: a\n    port: 6542\n"
        + terminal_lines
    )
    monkeypatch.setattr(helper, "CONFIG_PATH", str(cfg))
    terminal_dir = tmp_path / "terminal"
    assistant = _write_shipped(terminal_dir)
    monkeypatch.setattr(
        helper.sys, "argv",
        ["config_helper.py", "write_ini", "b", "a", str(terminal_dir / "mt5start.ini"), "default", "live"],
    )

    helper.main()

    assert (terminal_dir / "mt5start.ini").exists()
    assert _enabled(assistant) == expected


@pytest.mark.parametrize(("configured", "expected"), [(True, OFF), (False, ON)])
def test_an_api_restart_turns_it_off_before_relaunching(tmp_path, monkeypatch, configured, expected):
    assistant = _write_shipped(tmp_path)
    at_launch = {}

    def _launch(*_args, **_kwargs):
        at_launch.update(_enabled(assistant))
        return MagicMock()

    monkeypatch.setattr(mc, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(mc, "DISABLE_TERMINAL_MCP", configured)
    monkeypatch.setattr(mc, "m", lambda fn, *a, **kw: True)
    monkeypatch.setattr(mc, "_kill_terminal", lambda: True)
    monkeypatch.setattr(mc.subprocess, "Popen", _launch)
    monkeypatch.setattr(mc, "_wait_for_journal", lambda *a, **kw: True)
    monkeypatch.setattr(mc, "get_first_account", lambda: None)
    monkeypatch.setattr(mc, "init_mt5", lambda *a, **kw: False)

    mc.restart_terminal()

    assert at_launch == expected


@pytest.mark.parametrize(("configured", "expected"), [(True, OFF), (False, ON)])
def test_a_backtest_turns_it_off_before_launching_the_terminal(
    client, monkeypatch, configured, expected,  # noqa: F811 - fixture
):
    job_id = _submit(client)
    assistant = _write_shipped(handler.TERMINAL_DIR)
    at_launch = {}

    def _run(*_args, **_kwargs):
        at_launch.update(_enabled(assistant))
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr(handler, "DISABLE_TERMINAL_MCP", configured)
    monkeypatch.setattr(handler.subprocess, "run", _run)

    handler._execute_job(job_id)

    assert at_launch == expected
    assert os.path.exists(assistant)
