"""_tail_terminal_log picks the log that actually describes the run.

The terminal's logs/ directory holds `<date>.log` files plus a `metaeditor.log`
that is written once at install and never again. `metaeditor.log` sorts after
every dated log ("m" > "2"), so selecting by name attached a stale compile tail
to every backtest failure message — which is what hid an agent bind error
behind three-month-old MetaEditor output.
"""
from __future__ import annotations

import os
import time

import pytest

from mt5api.backtest import handler


def _write_utf16(path, text):
    with open(path, "w", encoding="utf-16-le") as handle:
        handle.write(text)


@pytest.fixture
def terminal_logs(monkeypatch, tmp_path):
    terminal_dir = tmp_path / "terminal"
    log_dir = terminal_dir / "logs"
    log_dir.mkdir(parents=True)
    monkeypatch.setattr(handler, "TERMINAL_DIR", str(terminal_dir))
    return log_dir


def _age(path, seconds_ago):
    when = time.time() - seconds_ago
    os.utime(path, (when, when))


def test_prefers_the_run_log_over_metaeditor_log(terminal_logs):
    _write_utf16(terminal_logs / "20260808.log", "Tester\tautomatic testing started\n")
    _write_utf16(terminal_logs / "metaeditor.log", "compiling ancient stuff\n")
    # metaeditor.log is both alphabetically last AND, here, newer on disk —
    # it must still never be chosen.
    _age(terminal_logs / "20260808.log", 3600)
    _age(terminal_logs / "metaeditor.log", 1)

    tail = handler._tail_terminal_log()
    assert "automatic testing started" in tail
    assert "ancient" not in tail


def test_picks_the_newest_dated_log(terminal_logs):
    _write_utf16(terminal_logs / "20260501.log", "old run\n")
    _write_utf16(terminal_logs / "20260808.log", "current run\n")
    _age(terminal_logs / "20260501.log", 90 * 86400)
    _age(terminal_logs / "20260808.log", 5)

    assert "current run" in handler._tail_terminal_log()


def test_newest_wins_even_when_it_sorts_first_by_name(terminal_logs):
    # A log rotated across a year boundary sorts before last year's file.
    _write_utf16(terminal_logs / "20261231.log", "last year\n")
    _write_utf16(terminal_logs / "20270101.log", "this year\n")
    _age(terminal_logs / "20261231.log", 86400)
    _age(terminal_logs / "20270101.log", 5)

    assert "this year" in handler._tail_terminal_log()


def test_returns_empty_when_only_metaeditor_log_exists(terminal_logs):
    _write_utf16(terminal_logs / "metaeditor.log", "compiling\n")
    assert handler._tail_terminal_log() == ""


def test_returns_empty_when_there_is_no_log_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "TERMINAL_DIR", str(tmp_path / "nothing-here"))
    assert handler._tail_terminal_log() == ""


def test_tail_is_limited_to_the_requested_line_count(terminal_logs):
    _write_utf16(terminal_logs / "20260808.log", "".join(f"line {i}\n" for i in range(50)))
    tail = handler._tail_terminal_log(lines=5)
    assert tail.splitlines() == [f"line {i}" for i in range(45, 50)]
