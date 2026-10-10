"""Turn off MetaTrader 5's own MCP servers before a terminal launches.

Terminal build 6090 and later starts two MCP servers of its own (the transport
behind the built-in AI assistant), configured in ``<terminal>/Config/assistant.ini``:
``[MCP.MetaTrader]`` on 127.0.0.1:22346 and ``[MCP.MetaEditor]`` on
127.0.0.1:22345. Every terminal in a VM shares loopback, so only the first can
bind the port and every other one logs ``MCP bind error on 127.0.0.1:22346
(10048)`` on each launch. The same subsystem signs in to MQL5.community, which a
backtest terminal has no account for, and backtest launches have aborted with
exit code 10053 right after ``MQL5.community authorization failed``. These are
not mt5-httpapi's own MCP endpoints (``mt5api/mcp_server.py``, ``mcpunifier``).

MT5 rewrites ``assistant.ini`` when it exits, so the file is rewritten before
every launch: at boot (``scripts/config_helper.py write_ini``), on a restart the
API makes (``mt5client.restart_terminal``) and before each backtest.

Opt-in with ``disable_terminal_mcp: true`` in config.yaml; a terminal's own
``disable_terminal_mcp: false`` keeps its servers. No imports from the rest of
the app, so the boot helper can load this module without the MT5 SDK.

Based on #29 by @Marinski.
"""
from __future__ import annotations

import configparser
import io
import logging
import os

# The app's logger by name (mt5api/logger.py configures it), so this module
# needs no app import.
log = logging.getLogger("mt5api")

ASSISTANT_INI = os.path.join("Config", "assistant.ini")
SECTIONS = ("MCP.MetaTrader", "MCP.MetaEditor")
CONFIG_KEY = "disable_terminal_mcp"
_ENABLE_KEY = "Enable"
_DISABLED = "0"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"


def wanted(global_value, terminal_entry) -> bool:
    """Whether this terminal's own MCP servers should be turned off.

    ``global_value`` is config.yaml's ``disable_terminal_mcp``; only ``True``
    turns it on. A terminal entry's ``disable_terminal_mcp: false`` opts that
    terminal out.
    """
    if global_value is not True:
        return False
    if isinstance(terminal_entry, dict) and terminal_entry.get(CONFIG_KEY) is False:
        return False
    return True


def _new_parser() -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    return parser


def _decode(raw: bytes) -> str:
    if raw.startswith(_UTF16_LE_BOM):
        text = raw.decode("utf-16-le", errors="replace")
    elif raw.startswith(_UTF16_BE_BOM):
        text = raw.decode("utf-16-be", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    # configparser refuses a first line that starts with U+FEFF.
    return text.lstrip("﻿")


def _read(path: str) -> configparser.RawConfigParser:
    """The file's sections, or none when it is missing or unparsable: a
    corrupt assistant.ini is replaced rather than allowed to block a launch."""
    parser = _new_parser()
    try:
        with open(path, "rb") as handle:
            text = _decode(handle.read())
    except FileNotFoundError:
        return parser
    if not text.strip():
        return parser
    try:
        parser.read_string(text)
    except configparser.Error:
        return _new_parser()
    return parser


def disable(terminal_dir: str) -> str:
    """Set ``Enable=0`` in both MCP sections of the terminal's assistant.ini.

    Keeps every other key and section (endpoints, API keys, custom servers). A
    missing or unparsable file is replaced by one holding just the two
    disabled sections. Writes UTF-16-LE with a BOM, as MT5 does. Returns the
    path written; raises OSError when it cannot be written.
    """
    path = os.path.join(terminal_dir, ASSISTANT_INI)
    parser = _read(path)
    for section in SECTIONS:
        if not parser.has_section(section):
            parser.add_section(section)
        parser.set(section, _ENABLE_KEY, _DISABLED)

    buffer = io.StringIO()
    parser.write(buffer, space_around_delimiters=False)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(_UTF16_LE_BOM)
        handle.write(buffer.getvalue().replace("\n", "\r\n").encode("utf-16-le"))
    return path


def apply(is_wanted: bool, terminal_dir: str, reason: str) -> None:
    """Disable the terminal's MCP servers when configured, right before a
    launch. ``reason`` names the launch (``backtest``, ``restart``) in the log.
    A failure is logged and never blocks the launch: a terminal with its MCP
    servers on still runs, as it always has."""
    if not is_wanted:
        return
    try:
        path = disable(terminal_dir)
    except OSError as err:
        log.warning("terminal MCP servers left on before %s launch: %s", reason, err)
        return
    log.info("terminal MCP servers disabled before %s launch (%s)", reason, path)
