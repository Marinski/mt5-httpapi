"""Behavioral tests for the log-rotator sidecar script."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "rotate-logs.sh"
_DEFAULT_TIMEOUT_SECONDS = 5
_POLL_INTERVAL_SECONDS = 0.05
_JOURNAL_LOCATIONS = (
    Path("logs"),
    Path("Tester/logs"),
    Path("Tester/Agent-127.0.0.1-3000/logs"),
)


def _journal_name(days_ago: int) -> str:
    date = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return date.strftime("%Y%m%d") + ".log"


def _write(path: Path, content: str = "fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _terminal_dir(terminals_dir: Path, relative_path: Path | None = None) -> Path:
    terminal_dir = terminals_dir / (relative_path or Path("broker/account/default"))
    _write(terminal_dir / "terminal64.exe", "")
    return terminal_dir


def _rotated_log_name(name: str) -> str:
    return name + "." + _journal_name(1).removesuffix(".log")


def _run_rotator(log_dir: Path, terminals_dir: Path, retain_days: str, ready) -> None:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LOG_DIR": str(log_dir),
        "TERMINALS_DIR": str(terminals_dir),
        "RETAIN_DAYS": retain_days,
        "INTERVAL": "3600",
    }
    process = subprocess.Popen(
        ["sh", str(_SCRIPT)],
        cwd=_REPO,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + _DEFAULT_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if ready():
                return
            exit_code = process.poll()
            if exit_code is None:
                time.sleep(_POLL_INTERVAL_SECONDS)
                continue
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"rotator exited {exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        raise AssertionError("rotator did not complete its first pass")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=_DEFAULT_TIMEOUT_SECONDS)


@pytest.mark.parametrize("location", _JOURNAL_LOCATIONS)
def test_prunes_expired_terminal_journal_at_each_supported_location(tmp_path, location):
    log_dir = tmp_path / "shared-logs"
    terminals_dir = tmp_path / "terminals"
    terminal_dir = _terminal_dir(terminals_dir)
    expired = terminal_dir / location / _journal_name(8)
    retained = terminal_dir / location / _journal_name(7)
    _write(expired)
    _write(retained)

    _run_rotator(log_dir, terminals_dir, "7", lambda: not expired.exists())

    assert not expired.exists()
    assert retained.exists()


def test_leaves_non_journal_logs_untouched(tmp_path):
    log_dir = tmp_path / "shared-logs"
    terminals_dir = tmp_path / "terminals"
    terminal = _terminal_dir(terminals_dir)
    expired_name = _journal_name(8)
    terminal_journal = terminal / "logs" / expired_name
    metaeditor_log = terminal / "logs/metaeditor.log"
    expert_log = terminal / "MQL5/Logs" / expired_name
    unrecognized_journal = terminals_dir / "other/logs" / expired_name
    _write(terminal_journal)
    _write(metaeditor_log)
    _write(expert_log)
    _write(unrecognized_journal)

    _run_rotator(log_dir, terminals_dir, "7", lambda: not terminal_journal.exists())

    assert not terminal_journal.exists()
    assert metaeditor_log.exists()
    assert expert_log.exists()
    assert unrecognized_journal.exists()


def test_configured_retention_window_overrides_the_default(tmp_path):
    log_dir = tmp_path / "shared-logs"
    terminals_dir = tmp_path / "terminals"
    journal_dir = _terminal_dir(terminals_dir) / _JOURNAL_LOCATIONS[0]
    expired = journal_dir / _journal_name(2)
    retained = journal_dir / _journal_name(1)
    _write(expired)
    _write(retained)

    _run_rotator(log_dir, terminals_dir, "1", lambda: not expired.exists())

    assert not expired.exists()
    assert retained.exists()


def test_prunes_journals_from_each_installed_terminal(tmp_path):
    log_dir = tmp_path / "shared-logs"
    terminals_dir = tmp_path / "terminals"
    terminal_dirs = (
        _terminal_dir(terminals_dir, Path("broker-one/account/default")),
        _terminal_dir(terminals_dir, Path("broker-two/account/default")),
    )
    journals = tuple(
        terminal_dir / _JOURNAL_LOCATIONS[0] / _journal_name(8)
        for terminal_dir in terminal_dirs
    )
    for journal in journals:
        _write(journal)

    _run_rotator(
        log_dir,
        terminals_dir,
        "7",
        lambda: all(not path.exists() for path in journals),
    )

    assert all(not path.exists() for path in journals)


def test_missing_terminals_root_does_not_block_shared_log_rotation(tmp_path):
    log_dir = tmp_path / "shared-logs"
    terminal_dir = tmp_path / "absent-terminals"
    live_log = log_dir / "full.log"
    _write(live_log)

    _run_rotator(
        log_dir,
        terminal_dir,
        "7",
        lambda: (log_dir / _rotated_log_name(live_log.name)).exists(),
    )

    assert live_log.read_text(encoding="utf-8") == ""
    assert (log_dir / _rotated_log_name(live_log.name)).read_text(
        encoding="utf-8"
    ) == "fixture\n"


def test_journal_cleanup_is_idempotent(tmp_path):
    log_dir = tmp_path / "shared-logs"
    terminals_dir = tmp_path / "terminals"
    journal = (
        _terminal_dir(terminals_dir) / _JOURNAL_LOCATIONS[0] / _journal_name(8)
    )
    _write(journal)

    _run_rotator(log_dir, terminals_dir, "7", lambda: not journal.exists())

    live_log = log_dir / "second-pass.log"
    _write(live_log)
    _run_rotator(
        log_dir,
        terminals_dir,
        "7",
        lambda: (log_dir / _rotated_log_name(live_log.name)).exists(),
    )

    assert not journal.exists()


@pytest.mark.parametrize("retain_days", ("-1", "0", "not-a-number"))
def test_invalid_retention_fails_before_deleting_journals(tmp_path, retain_days):
    log_dir = tmp_path / "shared-logs"
    terminals_dir = tmp_path / "terminals"
    journal = (
        _terminal_dir(terminals_dir) / _JOURNAL_LOCATIONS[0] / _journal_name(8)
    )
    _write(journal)

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LOG_DIR": str(log_dir),
        "TERMINALS_DIR": str(terminals_dir),
        "RETAIN_DAYS": retain_days,
        "INTERVAL": "3600",
    }
    result = subprocess.run(
        ["sh", str(_SCRIPT)],
        cwd=_REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode != 0
    assert journal.exists()
