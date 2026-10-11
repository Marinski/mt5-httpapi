"""Request logging must stay off the healthcheck's path.

The container healthcheck probes every API port on a VM at the same moment,
and each request log line takes full.log's cross-process lock on the Shared
folder. Logging those probes made an idle /ping take 1.1 s on a 45-terminal VM and
5-10 s under load, which the healthcheck reads as a hung port; the watchdog
then recreated a VM that was only busy, losing its running backtests.
"""
from __future__ import annotations

import logging

from mt5api import logger as mt5_logger
from mt5api.server import app


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _requests_logged(path):
    capture = _Capture()
    mt5_logger.log.addHandler(capture)
    try:
        app.config["TESTING"] = True
        app.test_client().get(path)
    finally:
        mt5_logger.log.removeHandler(capture)
    return [line for line in capture.lines if f" {path}" in line]


def test_ping_writes_no_request_log_lines():
    assert _requests_logged("/ping") == []


def test_other_requests_are_still_logged_both_ways():
    lines = _requests_logged("/no-such-route")

    assert any(" -> GET /no-such-route" in line for line in lines)
    assert any(" <- GET /no-such-route" in line and "dur_ms=" in line for line in lines)


def test_a_timed_out_lock_is_left_to_its_owner(tmp_path, monkeypatch):
    """After the ~2 s acquire window the line is still written, but the lock
    belongs to another process and must not be removed from under it."""
    monkeypatch.setattr(mt5_logger.time, "sleep", lambda _s: None)
    path = tmp_path / "full.log"
    handler = mt5_logger._LockedFileHandler(str(path), encoding="utf-8")
    lock_dir = tmp_path / "full.log.lock"
    lock_dir.mkdir()  # held by "another process"

    handler.emit(logging.makeLogRecord({"msg": "hello", "levelno": logging.INFO}))
    handler.close()

    assert "hello" in path.read_text()
    assert lock_dir.is_dir()


def test_a_taken_lock_is_released_after_the_write(tmp_path):
    path = tmp_path / "full.log"
    handler = mt5_logger._LockedFileHandler(str(path), encoding="utf-8")

    handler.emit(logging.makeLogRecord({"msg": "hello", "levelno": logging.INFO}))
    handler.close()

    assert "hello" in path.read_text()
    assert not (tmp_path / "full.log.lock").exists()
