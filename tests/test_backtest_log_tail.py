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


# ── Bounded reads (_read_tail_text) ─────────────────────────────────────
#
# Tailing must stay O(tail), never O(file): the live /tail endpoint is polled
# once a minute per running job against a Tester log that reaches gigabytes
# mid-run. A whole-file read of one of those took 45-65 s per call — decode
# and splitlines hold the GIL, so every thread in the process stalled,
# /ping and the container healthcheck included, and a healthy terminal
# looked wedged from the outside.


def test_read_tail_text_reads_only_the_final_window(tmp_path):
    path = tmp_path / "big.log"
    body = "".join(f"line {i:07d}\n" for i in range(200_000))  # ~2.6 MB utf-8
    path.write_text(body, encoding="utf-8")

    text = handler._read_tail_text(str(path), max_bytes=64 * 1024)

    lines = text.splitlines()
    assert lines[-1] == "line 0199999"
    assert len(text.encode("utf-8")) <= 64 * 1024
    # The window starts mid-file: the truncated first line must be dropped,
    # so every surviving line is complete.
    assert all(ln.startswith("line ") and len(ln) == 12 for ln in lines)


def test_read_tail_text_keeps_utf16_code_units_aligned(tmp_path):
    path = tmp_path / "terminal.log"
    body = "".join(f"запись {i:06d}\n" for i in range(50_000))  # force odd offsets
    with open(path, "w", encoding="utf-16-le") as fh:
        fh.write("﻿")
        fh.write(body)

    text = handler._read_tail_text(str(path), max_bytes=32 * 1024 + 1)

    lines = text.splitlines()
    assert lines[-1] == "запись 049999"
    # A misaligned seek shifts every code unit by one byte and turns the
    # whole tail to mojibake — one intact line proves alignment held.
    assert all(ln.startswith("запись ") for ln in lines)


def test_read_tail_text_small_file_is_returned_whole(tmp_path):
    path = tmp_path / "run.log"
    path.write_text("first\nsecond\n", encoding="utf-8")
    assert handler._read_tail_text(str(path)) == "first\nsecond\n"


def test_read_tail_text_missing_file_is_empty(tmp_path):
    assert handler._read_tail_text(str(tmp_path / "absent.log")) == ""


def test_tail_terminal_log_is_bounded(terminal_logs):
    huge = "".join(f"entry {i:08d}\n" for i in range(300_000))  # ~9.7 MB utf-16
    _write_utf16(terminal_logs / "20260829.log", huge)

    tail = handler._tail_terminal_log(lines=5)

    assert tail.splitlines() == [f"entry {i:08d}" for i in range(299_995, 300_000)]


# ── _tail_dir_log (the live /tail endpoint's picker) ────────────────────
#
# Same two rules as _tail_terminal_log — newest by mtime, never
# metaeditor.log — which this helper predated and never received.


def test_tail_dir_log_never_picks_metaeditor_log(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_utf16(log_dir / "20260808.log", "Tester\tautomatic testing started\n")
    _write_utf16(log_dir / "metaeditor.log", "compiling ancient stuff\n")
    _age(log_dir / "20260808.log", 3600)
    _age(log_dir / "metaeditor.log", 1)

    path, tail = handler._tail_dir_log(str(log_dir), 20)

    assert path.endswith("20260808.log")
    assert "ancient" not in tail


def test_tail_dir_log_picks_newest_by_mtime_not_name(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_utf16(log_dir / "20261231.log", "last year\n")
    _write_utf16(log_dir / "20270101.log", "this year\n")
    _age(log_dir / "20261231.log", 5)       # older name, newer mtime
    _age(log_dir / "20270101.log", 86400)

    path, tail = handler._tail_dir_log(str(log_dir), 20)

    assert path.endswith("20261231.log")
    assert tail == "last year"


def test_tail_dir_log_is_bounded_on_a_large_log(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    huge = "".join(f"tick {i:08d}\n" for i in range(300_000))
    _write_utf16(log_dir / "20260829.log", huge)

    _, tail = handler._tail_dir_log(str(log_dir), 3)

    assert tail.splitlines() == [f"tick {i:08d}" for i in range(299_997, 300_000)]


def test_tail_dir_log_empty_dir(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    assert handler._tail_dir_log(str(log_dir), 20) == (None, "")
