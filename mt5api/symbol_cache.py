"""Persisted broker symbol names, so the backtest INI builder can tell a real
symbol from one that still needs the broker's suffix.

`_normalize_symbol` in mt5api/backtest/handler.py appends `symbol_suffix` to
`[Tester].Symbol`. Brokers rarely suffix everything: Eightcap Global carries
56 suffixed FX pairs (`EURUSD.i`) alongside 785 bare ones (`XAUUSD`, `BTCUSD`,
`ASX200`), so appending unconditionally invents names that do not exist.

A `mode: backtest` terminal never attaches the MT5 SDK — mt5api/main.py skips
init so the tester can own the data dir — so there is no live way to ask the
broker which names exist at INI-build time. `Bases/<server>/symbols/*.dat` is
encrypted and unreadable. This module is the workaround: GET /symbols (on a
terminal that DOES attach the SDK) writes the full list it saw, and the INI
builder reads it back.

GET /symbols itself calls the SDK (`ensure_initialized()`), so it is refused
on a `mode: backtest` terminal instead of triggering a full `mt5.initialize()`
that would spawn `terminal64.exe` and hold the tester's single-instance lock.
POST /symbols/import (mt5api/handlers/symbols.py) is the safe priming path for
those terminals: it calls symbol_cache.save() directly from a caller-supplied
list and never touches mt5.*.

The cache is only ever used to SUPPRESS a remap that would invent a symbol.
When it is missing, stale or unreadable the builder falls back to appending,
which is the behaviour that shipped before it existed. Staleness is enforced
in load() itself — a cache older than MAX_AGE_SECONDS, or one without a valid
``updated`` stamp, is treated exactly like no cache — so no caller can forget
the check and treat an ancient list as current.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from mt5api.config import SYMBOL_CACHE_MAX_AGE_SECONDS
from mt5api.logger import log

CACHE_BASENAME = "mt5api-symbols.json"

#: Module-level so tests (and an operator poking at a live process) can see
#: and override what load() enforces. Config default: 7 days, via
#: SYMBOL_CACHE_MAX_AGE / symbol_cache_max_age.
MAX_AGE_SECONDS = SYMBOL_CACHE_MAX_AGE_SECONDS


def cache_path(terminal_dir):
    return os.path.join(terminal_dir, CACHE_BASENAME)


def save(terminal_dir, names):
    """Persist the broker's full symbol list. Best-effort: never raises.

    Written atomically — the INI builder reads this on every backtest, and a
    torn file would silently degrade every remap decision until overwritten.
    """
    names = sorted({str(n) for n in names if n})
    if not names:
        return False
    path = cache_path(terminal_dir)
    payload = {"updated": int(time.time()), "symbols": names}
    try:
        os.makedirs(terminal_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=terminal_dir, prefix=".symbols-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, path)
        except BaseException:
            # Leaving a .tmp behind on every failed write would slowly fill the
            # terminal dir; the replace above is what makes this safe to drop.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("symbol cache write failed at %s: %s", path, exc)
        return False
    return True


def load(terminal_dir, max_age_seconds=None):
    """Return the cached symbol set, or None when there is no usable cache.

    None means "no opinion" — callers must treat it as unknown, not empty.

    A cache past ``max_age_seconds`` (default: MAX_AGE_SECONDS) is unusable,
    and so is one whose ``updated`` stamp is missing or malformed. This is
    what keeps the cache from being AUTHORITATIVE forever: a broker that
    moves a symbol between bare and suffixed would otherwise keep being
    normalized against a years-old list until someone happened to call
    GET /symbols. Stale degrades to the append-always fallback — the
    conservative behaviour that shipped before the cache existed — never to
    a wrong answer presented as a current one.
    """
    limit = MAX_AGE_SECONDS if max_age_seconds is None else max_age_seconds
    path = cache_path(terminal_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("symbol cache unreadable at %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        log.warning("symbol cache at %s has no symbols list", path)
        return None
    updated = payload.get("updated")
    if not isinstance(updated, int) or isinstance(updated, bool) or updated <= 0:
        log.warning(
            "symbol cache at %s has a missing or invalid 'updated' stamp; "
            "treating as stale", path,
        )
        return None
    age = int(time.time()) - updated
    if age > limit:
        # Almost every reader of this cache is a mode:backtest terminal, where
        # GET /symbols is refused with 409 — so naming it here would send the
        # operator at the one call that cannot work.
        log.warning(
            "symbol cache at %s is %ds old (max %ds); treating as stale — "
            "refresh it with POST /symbols/import on this terminal, or "
            "GET /symbols if this terminal is mode:live", path, age, limit,
        )
        return None
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        log.warning("symbol cache at %s has no symbols list", path)
        return None
    return {str(s) for s in symbols}


def age_seconds(terminal_dir):
    """Seconds since the cache was written, or None when absent/unreadable."""
    path = cache_path(terminal_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        updated = int(payload["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return max(0, int(time.time()) - updated)
