"""Tests for the suffix remap in mt5api.backtest.handler._normalize_symbol
and the symbol cache it consults.

The remap exists because config.yaml carries one `symbol_suffix` per terminal,
but real brokers suffix only part of their book. The cache is what lets the
builder tell those apart; without it the remap must keep its old behaviour.
"""
from __future__ import annotations

import configparser
import json
import os

import pytest

from mt5api import symbol_cache
from mt5api.backtest import handler


def _parser(symbol):
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser["Tester"] = {"Symbol": symbol}
    return parser


@pytest.fixture
def terminal_dir(monkeypatch, tmp_path):
    d = tmp_path / "terminal"
    d.mkdir()
    monkeypatch.setattr(handler, "TERMINAL_DIR", str(d))
    monkeypatch.setattr(handler, "BROKER", "eightcapglobal")
    monkeypatch.setattr(handler, "ACCOUNT", "live-raw")
    return d


def _configure(monkeypatch, suffix):
    monkeypatch.setattr(handler, "SYMBOL_SUFFIX", suffix)
    monkeypatch.setattr(handler, "SYMBOL_SUFFIX_CONFIGURED", True)


def _remap(symbol):
    parser = _parser(symbol)
    handler._normalize_symbol(parser)
    return parser["Tester"]["Symbol"]


# ── Without a cache: unchanged legacy behaviour ──────────────────────


def test_appends_suffix_when_no_cache_exists(terminal_dir, monkeypatch):
    _configure(monkeypatch, ".i")
    assert _remap("EURUSD") == "EURUSD.i"


def test_appends_suffix_when_cache_is_corrupt(terminal_dir, monkeypatch):
    _configure(monkeypatch, ".i")
    symbol_cache.cache_path(str(terminal_dir))
    with open(symbol_cache.cache_path(str(terminal_dir)), "w") as fh:
        fh.write("{not json")
    assert _remap("XAUUSD") == "XAUUSD.i"


def test_no_suffix_configured_leaves_symbol_alone(terminal_dir, monkeypatch):
    monkeypatch.setattr(handler, "SYMBOL_SUFFIX", "")
    monkeypatch.setattr(handler, "SYMBOL_SUFFIX_CONFIGURED", True)
    assert _remap("EURUSD") == "EURUSD"


def test_already_suffixed_symbol_is_not_double_suffixed(terminal_dir, monkeypatch):
    _configure(monkeypatch, ".i")
    symbol_cache.save(str(terminal_dir), ["EURUSD.i"])
    assert _remap("EURUSD.i") == "EURUSD.i"


# ── With a cache: the actual fix ─────────────────────────────────────


def test_bare_only_symbol_keeps_its_name(terminal_dir, monkeypatch):
    """Eightcap: XAUUSD exists, XAUUSD.i does not — appending invents a symbol."""
    _configure(monkeypatch, ".i")
    symbol_cache.save(str(terminal_dir), ["EURUSD.i", "XAUUSD", "ASX200", "BTCUSD"])
    assert _remap("XAUUSD") == "XAUUSD"
    assert _remap("ASX200") == "ASX200"
    assert _remap("BTCUSD") == "BTCUSD"


def test_suffixed_symbol_is_still_remapped(terminal_dir, monkeypatch):
    """Eightcap: EURUSD does not exist, EURUSD.i does."""
    _configure(monkeypatch, ".i")
    symbol_cache.save(str(terminal_dir), ["EURUSD.i", "XAUUSD"])
    assert _remap("EURUSD") == "EURUSD.i"


def test_suffix_wins_when_broker_carries_both_forms(terminal_dir, monkeypatch):
    """BlackBull lists AUDUSD and AUDUSDp. `symbol_suffix: p` means prime, so
    the suffixed variant must still win — this is why the guard requires the
    suffixed form to be ABSENT, not merely the bare form to be present."""
    _configure(monkeypatch, "p")
    monkeypatch.setattr(handler, "BROKER", "blackbull")
    symbol_cache.save(str(terminal_dir), ["AUDUSD", "AUDUSDp", "XAUUSD"])
    assert _remap("AUDUSD") == "AUDUSDp"


def test_unknown_symbol_falls_back_to_appending(terminal_dir, monkeypatch):
    """Neither form cached — a stale cache must not block a valid new listing."""
    _configure(monkeypatch, ".i")
    symbol_cache.save(str(terminal_dir), ["EURUSD.i", "XAUUSD"])
    assert _remap("NEWPAIR") == "NEWPAIR.i"


def test_empty_symbol_is_left_alone(terminal_dir, monkeypatch):
    _configure(monkeypatch, ".i")
    assert _remap("") == ""


# ── Cache module ─────────────────────────────────────────────────────


def test_save_then_load_roundtrips(tmp_path):
    d = str(tmp_path)
    assert symbol_cache.save(d, ["EURUSD.i", "XAUUSD"]) is True
    assert symbol_cache.load(d) == {"EURUSD.i", "XAUUSD"}


def test_load_returns_none_when_absent(tmp_path):
    assert symbol_cache.load(str(tmp_path)) is None


def test_save_refuses_an_empty_list(tmp_path):
    """An empty write would turn 'unknown' into 'nothing exists' and suppress
    every remap."""
    assert symbol_cache.save(str(tmp_path), []) is False
    assert symbol_cache.load(str(tmp_path)) is None


def test_save_leaves_no_temp_files_behind(tmp_path):
    d = str(tmp_path)
    symbol_cache.save(d, ["EURUSD.i"])
    leftovers = [n for n in os.listdir(d) if n.endswith(".tmp")]
    assert leftovers == []


def test_save_is_atomic_over_an_existing_cache(tmp_path):
    d = str(tmp_path)
    symbol_cache.save(d, ["OLD"])
    symbol_cache.save(d, ["NEW1", "NEW2"])
    assert symbol_cache.load(d) == {"NEW1", "NEW2"}


def test_cache_records_when_it_was_written(tmp_path):
    d = str(tmp_path)
    symbol_cache.save(d, ["EURUSD.i"])
    with open(symbol_cache.cache_path(d)) as fh:
        payload = json.load(fh)
    assert isinstance(payload["updated"], int)
    assert symbol_cache.age_seconds(d) is not None
    assert symbol_cache.age_seconds(d) < 5


# ── Staleness: the cache must not be authoritative forever ───────────


def _age_cache(d, updated):
    """Rewrite the cache's updated stamp in place."""
    path = symbol_cache.cache_path(d)
    with open(path) as fh:
        payload = json.load(fh)
    payload["updated"] = updated
    with open(path, "w") as fh:
        json.dump(payload, fh)


def test_a_stale_cache_is_no_cache(tmp_path):
    """load() itself enforces the age. Enforcing it anywhere else means a
    caller can forget to, which is exactly what production did: age_seconds()
    existed and nothing called it."""
    d = str(tmp_path)
    symbol_cache.save(d, ["XAUUSD"])
    _age_cache(d, 1)  # epoch: ~1.7 billion seconds old
    assert symbol_cache.load(d) is None


def test_a_cache_missing_its_timestamp_is_no_cache(tmp_path):
    d = str(tmp_path)
    symbol_cache.save(d, ["XAUUSD"])
    path = symbol_cache.cache_path(d)
    with open(path) as fh:
        payload = json.load(fh)
    del payload["updated"]
    with open(path, "w") as fh:
        json.dump(payload, fh)
    assert symbol_cache.load(d) is None


@pytest.mark.parametrize("updated", ["yesterday", None, -5, 0, 3.7, True])
def test_a_cache_with_a_malformed_timestamp_is_no_cache(tmp_path, updated):
    d = str(tmp_path)
    symbol_cache.save(d, ["XAUUSD"])
    _age_cache(d, updated)
    assert symbol_cache.load(d) is None


def test_a_cache_within_the_window_is_served(tmp_path):
    d = str(tmp_path)
    symbol_cache.save(d, ["XAUUSD"])
    assert symbol_cache.load(d) == {"XAUUSD"}


def test_the_age_limit_is_configurable_per_call(tmp_path, monkeypatch):
    import time as _time
    d = str(tmp_path)
    symbol_cache.save(d, ["XAUUSD"])
    _age_cache(d, int(_time.time()) - 120)
    assert symbol_cache.load(d, max_age_seconds=60) is None
    assert symbol_cache.load(d, max_age_seconds=3600) == {"XAUUSD"}


def test_a_stale_bare_symbol_cache_falls_back_to_the_suffix(
    terminal_dir, monkeypatch
):
    """The integration-level case from review, through the real INI builder
    path: a stale cache that knows XAUUSD as bare must NOT keep suppressing
    the remap — the broker may have moved the symbol since. Stale means the
    conservative append-always fallback, same as no cache at all."""
    _configure(monkeypatch, ".i")
    symbol_cache.save(str(terminal_dir), ["XAUUSD"])

    # Fresh cache first, proving the suppression is live and the staleness is
    # what flips it — not some other reason to append.
    assert _remap("XAUUSD") == "XAUUSD"

    _age_cache(str(terminal_dir), 1)  # epoch
    assert _remap("XAUUSD") == "XAUUSD.i"
