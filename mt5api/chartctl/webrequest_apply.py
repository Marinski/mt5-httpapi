"""Apply the WebRequest allowlist to the running terminal in the VM.

A terminal started with ``/config:<file>`` (every terminal in the VM) never
writes its settings back, so it forgets the list each time terminal64.exe
starts. The API therefore sets the list the way a user would, in the
terminal's own Options dialog, with window messages (winoptions), and
re-applies it after the API starts and after every terminal restart it
performs. A bare-metal terminal, where common.ini is the store, keeps the
common.ini rewrite plus restart in handlers/webrequest.py.

Every apply records its outcome in ``Config/webrequest_status.json``, which
``GET /webrequest`` returns as ``last_apply``: the background re-apply gives
up after a few attempts and would otherwise only leave a log line.

No GUI call runs while a backtest holds RUN_LOCK: the tester's process runs
from the same terminal64.exe, so the pid lookup would find it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from mt5api.chartctl import terminal_pid, winoptions
from mt5api.chartctl import webrequest as wr
from mt5api.chartctl.terminal_pid import TerminalPidError
from mt5api.chartctl.winoptions import OptionsError
from mt5api.config import PORT, TERMINAL_DIR
from mt5api.logger import log

STATUS_OK = "OK"
STATUS_NAME = "webrequest_status.json"
CODE_BACKTEST_RUNNING = "backtest_running"
CODE_INTERNAL = "internal_error"

_REAPPLY_ATTEMPTS = 3
_REAPPLY_RETRY_SECONDS = 15
# How often a re-apply blocked by a running backtest looks again. A wait is
# not one of its attempts: a backtest can run for hours.
_BACKTEST_POLL_SECONDS = 30
# Seconds to let a freshly started terminal's GUI settle before driving it,
# plus a per-port stagger so one VM's terminals do not all open their Options
# dialogs in the same second at boot.
_GUI_SETTLE_SECONDS = 25
_GUI_SETTLE_STAGGER_SECONDS = 3
_GUI_SETTLE_STAGGER_SLOTS = 10


def available() -> bool:
    """True on Windows (the VM), where the terminal's windows can be driven.
    Elsewhere the caller uses the common.ini fallback."""
    return os.name == "nt"


def backtest_running() -> bool:
    # Imported here: the backtest handler pulls in Flask views this module
    # does not otherwise need.
    from mt5api.backtest.handler import RUN_LOCK

    return RUN_LOCK.locked()


def gui_settle_seconds() -> int:
    """How long to wait after a terminal starts before driving its GUI."""
    stagger = (PORT % _GUI_SETTLE_STAGGER_SLOTS) * _GUI_SETTLE_STAGGER_SECONDS
    return _GUI_SETTLE_SECONDS + stagger


def _cfg_dir() -> str:
    return wr.config_dir(TERMINAL_DIR)


def status_path() -> str:
    return os.path.join(_cfg_dir(), STATUS_NAME)


def load_status() -> dict | None:
    try:
        with open(status_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_status(reason: str, status: str, code: str | None, rejected: list[str]) -> None:
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "status": STATUS_OK if status == STATUS_OK else "FAIL",
        "code": code,
        "rejected": rejected,
    }
    path = status_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("WebRequest: could not write %s (%s)", path, exc)


def _drive(add: list[str], remove: list[str], exact: list[str] | None, partial: bool) -> dict:
    if backtest_running():
        raise OptionsError(
            CODE_BACKTEST_RUNNING, "a backtest is running on this terminal; try again after it"
        )
    pid, _started = terminal_pid.find_terminal()
    return winoptions.apply(pid, add, remove, exact=exact, partial=partial)


def _run(
    reason: str, add: list[str], remove: list[str], exact: list[str] | None, partial: bool
) -> tuple[str, dict]:
    """(status, detail): status "OK" or "FAIL:<code>"; detail is the driver's
    result on OK, else {"error", "diag"}. Never raises."""
    try:
        result = _drive(add, remove, exact, partial)
    except (OptionsError, TerminalPidError) as err:
        log.warning(
            "WebRequest apply (%s) failed: %s %s diag=%s",
            reason,
            err.code,
            err.message,
            json.dumps(err.diag)[:800],
        )
        _write_status(reason, "FAIL", err.code, [])
        return f"FAIL:{err.code}", {"error": err.message, "diag": err.diag}
    except Exception:  # noqa: BLE001 - a ctypes or OS failure must answer, not kill the caller
        log.exception("WebRequest apply (%s) failed unexpectedly", reason)
        _write_status(reason, "FAIL", CODE_INTERNAL, [])
        return f"FAIL:{CODE_INTERNAL}", {
            "error": "the apply failed unexpectedly; see the API log",
            "diag": {},
        }
    rejected = result.get("rejected", [])
    log.info(
        "WebRequest apply (%s): OK enabled=%s urls=%d added=%d removed=%d rejected=%d",
        reason,
        result.get("enabled"),
        len(result.get("urls", [])),
        len(result.get("added", [])),
        len(result.get("removed", [])),
        len(rejected),
    )
    _write_status(reason, STATUS_OK, None, rejected)
    return STATUS_OK, result


def apply_urls(urls: list[str], *, partial: bool = False, reason: str = "put") -> tuple[str, str]:
    """Make the terminal's list exactly ``urls``. Returns (status, json detail)."""
    status, detail = _run(reason, [], [], list(urls), partial)
    return status, json.dumps(detail)


def patch_urls(add: list[str], remove: list[str]) -> tuple[str, str, list[str] | None]:
    """Add and remove only the named URLs, keeping every other row. Returns
    (status, json detail, the URL rows read back on OK else None)."""
    status, detail = _run("patch", list(add), list(remove), None, False)
    return status, json.dumps(detail), (detail.get("urls") if status == STATUS_OK else None)


def read_live() -> tuple[str, dict]:
    """Read the live list without changing it.

    Returns (status, {"enabled", "urls"}) on OK, else (status, {"error", "diag"})."""
    status, detail = _run("verify", [], [], None, False)
    if status != STATUS_OK:
        return status, detail
    return status, {"enabled": detail.get("enabled"), "urls": detail.get("urls", [])}


def reapply_in_background(delay_seconds: float, reason: str) -> None:
    """Re-apply the stored allowlist on a daemon thread, after `delay_seconds`
    for the terminal's GUI to settle.

    Partial: a stored URL MT5 will not take is skipped and reported, so one bad
    entry does not leave the terminal with no allowlist at all. No-op off
    Windows (bare metal keeps the list in common.ini) or without a stored list.
    """
    if not available():
        return
    urls = wr.effective_urls(_cfg_dir())
    if not urls:
        return

    def _work():
        try:
            time.sleep(delay_seconds)
            attempt = 0
            while attempt < _REAPPLY_ATTEMPTS:
                if backtest_running():
                    time.sleep(_BACKTEST_POLL_SECONDS)
                    continue
                attempt += 1
                status, _detail = apply_urls(urls, partial=True, reason=reason)
                log.info(
                    "WebRequest re-apply (%s) attempt %d: %d url(s) -> %s",
                    reason,
                    attempt,
                    len(urls),
                    status,
                )
                if status == STATUS_OK:
                    return
                time.sleep(_REAPPLY_RETRY_SECONDS)
            log.warning(
                "WebRequest re-apply (%s) gave up after %d attempts; "
                "POST /webrequest/apply retries it",
                reason,
                _REAPPLY_ATTEMPTS,
            )
        except Exception:  # noqa: BLE001 - a daemon thread must log, not die silently
            log.exception("WebRequest re-apply (%s) failed", reason)

    threading.Thread(target=_work, name="webrequest-reapply", daemon=True).start()
