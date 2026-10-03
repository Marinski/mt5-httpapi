"""Tests for per-terminal MetaTester agent-port isolation (Fix 1).

Every terminal defaults its local agents to 127.0.0.1:3000 upward, so two
terminals running concurrently in one Windows VM collide ("bind error on
127.0.0.1:300x [10048]"); the pass dies instantly and the job comes back
"Report not generated". These tests pin the allocation rule that gives every
terminal a distinct block and the [Tester] Port key that carries it into a run.
"""
from __future__ import annotations

from mt5api.backtest import handler
from mt5api.config import (
    AGENT_PORT_BASE_DEFAULT,
    AGENT_PORT_BLOCK,
    agent_port_base,
    instance_ordinal,
)


def test_default_instance_keeps_the_historical_base():
    assert instance_ordinal("default") == 0
    assert agent_port_base("default") == AGENT_PORT_BASE_DEFAULT


def test_single_letters_are_sequential_and_blocked():
    assert agent_port_base("a") == AGENT_PORT_BASE_DEFAULT + AGENT_PORT_BLOCK
    assert agent_port_base("b") == AGENT_PORT_BASE_DEFAULT + 2 * AGENT_PORT_BLOCK
    # a..z strictly increase by one block each.
    ports = [agent_port_base(chr(ord("a") + i)) for i in range(26)]
    assert ports == sorted(ports)
    assert len(set(ports)) == 26


def test_no_collisions_across_the_live_fleet_labels():
    labels = ["default"] + [chr(ord("a") + i) for i in range(26)] + ["aa", "ab", "ac", "ad"]
    ports = [agent_port_base(label) for label in labels]
    assert len(set(ports)) == len(ports), "two terminals would share an agent-port base"


def test_multi_letter_labels_band_after_z():
    # "aa" must be beyond "z" and distinct from every single letter.
    assert agent_port_base("aa") > agent_port_base("z")
    assert instance_ordinal("aa") == 27


def test_numeric_labels_use_the_number_itself():
    """Numeric instances (the backend's preferred scheme) must map to small,
    sane blocks: "22" -> 3880, not the multi-letter hash band. A huge Port
    (e.g. 107000) is rejected by MT5 and the pass dies with "10053" /
    "Report not generated" — the 2026-10-03 regression."""
    assert instance_ordinal("1") == 1
    assert instance_ordinal("22") == 22
    assert instance_ordinal("60") == 60
    assert agent_port_base("22") == AGENT_PORT_BASE_DEFAULT + 22 * AGENT_PORT_BLOCK
    # 1..60 stay inside one sane band and never collide.
    ports = [agent_port_base(str(n)) for n in range(1, 61)]
    assert len(set(ports)) == 60
    assert max(ports) < 10000


def test_env_override_wins():
    assert agent_port_base("a", "3100") == 3100
    # An unparsable override falls back to the derived value, never crashes.
    assert agent_port_base("a", "not-a-port") == AGENT_PORT_BASE_DEFAULT + AGENT_PORT_BLOCK


def test_apply_agent_port_writes_the_tester_port_key(monkeypatch):
    import configparser

    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read_string("[Tester]\nSymbol=EURUSD\n")
    monkeypatch.setattr(handler, "INSTANCE", "c")
    handler._apply_agent_port(parser)
    assert parser["Tester"]["Port"] == str(
        AGENT_PORT_BASE_DEFAULT + 3 * AGENT_PORT_BLOCK
    )
