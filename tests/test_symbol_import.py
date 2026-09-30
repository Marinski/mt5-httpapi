"""HTTP-level regression tests for the safe symbol-cache priming path.

GET /symbols is @with_mt5 and calls ensure_initialized(), which on a terminal
that never attached the SDK (mode: backtest) falls through to a full
mt5.initialize() -- spawning terminal64.exe and holding the tester's
single-instance data-dir lock for the rest of that terminal's life. That used
to be the documented way to prime this terminal's symbol cache; it wedged the
terminal instead. These tests prove, against the real Flask app:

  1. GET /symbols on a mode: backtest terminal is refused BEFORE
     ensure_initialized() or any mt5.* SDK call is ever reached.
  2. POST /symbols/import -- the safe replacement -- writes the cache without
     calling ensure_initialized() or any mt5.* SDK call, regardless of mode.
  3. That refusal does not queue behind the global MT5 lock, and the live
     listing still does.
  4. The priming workflow config/config.yaml.example documents actually
     produces the cache the backtest INI builder reads.
  5. The three per-request caps hold: body bytes (refused BEFORE the body is
     parsed), entry count, and per-symbol length.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import mt5api.handlers.symbols as h
from mt5api import config, mt5client, symbol_cache
from mt5api.backtest import handler as backtest_handler
from mt5api.server import app


def _forbidden(*_args, **_kwargs):
    raise AssertionError("ensure_initialized() must not be called here")


def _client():
    app.config["TESTING"] = True
    return app.test_client()


def _cached(terminal_dir):
    """The names in the cache file, complete or not; None when nothing was written."""
    try:
        with open(symbol_cache.cache_path(terminal_dir), encoding="utf-8") as handle:
            return set(json.load(handle)["symbols"])
    except FileNotFoundError:
        return None


def test_list_symbols_refused_on_backtest_mode_without_sdk_call(monkeypatch):
    monkeypatch.setattr(h, "MODE", "backtest")
    monkeypatch.setattr(h, "ensure_initialized", _forbidden)
    h.mt5.symbols_get.reset_mock()

    resp = _client().get("/symbols")

    assert resp.status_code == 409
    body = resp.get_json()
    assert body is not None and "error" in body
    assert "backtest" in body["error"].lower()
    h.mt5.symbols_get.assert_not_called()


def test_list_symbols_still_works_live_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "MODE", "live")
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "ensure_initialized", lambda: True)
    fake = type("S", (), {"name": "EURUSD"})()
    monkeypatch.setattr(h.mt5, "symbols_get", lambda **kw: [fake])

    resp = _client().get("/symbols")

    assert resp.status_code == 200
    assert resp.get_json() == ["EURUSD"]
    assert symbol_cache.load(str(tmp_path)) == {"EURUSD"}


def test_import_symbols_writes_cache_without_sdk_call(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "MODE", "backtest")
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "ensure_initialized", _forbidden)
    h.mt5.initialize.reset_mock()
    h.mt5.terminal_info.reset_mock()

    resp = _client().post("/symbols/import", json={"symbols": ["EURUSD", "XAUUSD", "EURUSD"]})

    assert resp.status_code == 200
    assert resp.get_json()["imported"] == 2
    assert _cached(str(tmp_path)) == {"EURUSD", "XAUUSD"}
    h.mt5.initialize.assert_not_called()
    h.mt5.terminal_info.assert_not_called()


def test_import_symbols_strips_stray_whitespace(monkeypatch, tmp_path):
    # This is operator copy-paste input, not the SDK-sourced list GET /symbols
    # persists -- a stray space would silently defeat the exact-match lookup
    # in backtest.handler._normalize_symbol.
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={"symbols": [" EURUSD ", "XAUUSD\n", "   "]})

    assert resp.status_code == 200
    assert resp.get_json()["imported"] == 2
    assert _cached(str(tmp_path)) == {"EURUSD", "XAUUSD"}


def test_import_symbols_rejects_non_list_body(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={"symbols": "EURUSD"})

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


def test_import_symbols_rejects_non_string_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={"symbols": ["EURUSD", 123]})

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


def test_import_symbols_rejects_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={"symbols": []})

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


def test_import_symbols_rejects_missing_body(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={})

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


def test_import_symbols_rejects_bare_json_array_body(monkeypatch, tmp_path):
    # get_json(silent=True) returns the bare list as-is for syntactically
    # valid JSON that isn't an object -- a naive body.get("symbols") on that
    # raises AttributeError (500) instead of the clean 400 every other bad
    # shape gets. Regression for that crash.
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json=["EURUSD", "XAUUSD"])

    assert resp.status_code == 400
    body = resp.get_json()
    assert body is not None and "error" in body
    assert _cached(str(tmp_path)) is None


def test_import_symbols_rejects_bare_json_scalar_body(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json=42)

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


def test_import_symbols_rejects_no_body_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import")

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


# ── The refusal must not queue behind the global MT5 lock ────────────

@pytest.fixture
def mt5_lock_held():
    """Hold the global MT5 lock from another thread, as an in-flight SDK
    request would, and shorten the acquire timeout so a handler that queues
    for it fails in milliseconds instead of the production minute.
    """
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        mt5client._mt5_lock.acquire()
        acquired.set()
        release.wait(timeout=30)
        mt5client._mt5_lock.release()

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert acquired.wait(timeout=5), "lock holder thread never started"
    original = mt5client.SESSION_ACQUIRE_TIMEOUT
    mt5client.SESSION_ACQUIRE_TIMEOUT = 2.0
    try:
        yield
    finally:
        mt5client.SESSION_ACQUIRE_TIMEOUT = original
        release.set()
        thread.join(timeout=5)


def test_backtest_refusal_does_not_wait_on_the_mt5_lock(monkeypatch, mt5_lock_held):
    """A backtest terminal whose SDK request is stuck must still refuse GET
    /symbols immediately. While @with_mt5 wrapped this handler the decorator
    entered session() and blocked on the lock before the MODE check ever ran,
    so the caller got a 503 after the full acquire timeout -- 60s in
    production -- instead of the 409 that needs no SDK at all.
    """
    monkeypatch.setattr(h, "MODE", "backtest")
    monkeypatch.setattr(h, "ensure_initialized", _forbidden)

    started = time.monotonic()
    resp = _client().get("/symbols")
    elapsed = time.monotonic() - started

    assert resp.status_code == 409, (
        f"expected an immediate 409, got {resp.status_code} -- the refusal "
        "queued for the MT5 lock"
    )
    assert elapsed < 1.0, f"refusal took {elapsed:.3f}s; it waited on the lock"
    # The holder still owns it: the request neither took nor stole the lock.
    assert mt5client._mt5_lock.locked()


def test_live_listing_still_waits_on_the_mt5_lock(monkeypatch, mt5_lock_held):
    """The other half of the split: only the backtest branch skips the lock.
    The live path still touches the SDK, so it must stay serialized behind it
    rather than racing other handlers.
    """
    monkeypatch.setattr(h, "MODE", "live")
    monkeypatch.setattr(h, "ensure_initialized", _forbidden)

    resp = _client().get("/symbols")

    assert resp.status_code == 503
    assert "could not acquire MT5 lock" in resp.get_json()["error"]


# ── The workflow config/config.yaml.example documents ────────────────

def test_documented_backtest_priming_workflow_primes_the_suffix_decision(
    monkeypatch, tmp_path
):
    """Walk the flow config/config.yaml.example tells a backtest-mode operator
    to run, and assert the outcome it promises: after priming, the INI builder
    stops appending the suffix to a symbol the broker carries bare.

    Source of the list is a live terminal on the same broker/account, so the
    payload here is exactly what GET /symbols returns there.
    """
    terminal = tmp_path / "terminal"
    terminal.mkdir()
    monkeypatch.setattr(h, "TERMINAL_DIR", str(terminal))
    monkeypatch.setattr(backtest_handler, "TERMINAL_DIR", str(terminal))
    monkeypatch.setattr(backtest_handler, "SYMBOL_SUFFIX", ".i")
    monkeypatch.setattr(backtest_handler, "SYMBOL_SUFFIX_CONFIGURED", True)
    client = _client()

    # Step 1: the old instruction -- GET /symbols -- is refused here.
    monkeypatch.setattr(h, "MODE", "backtest")
    monkeypatch.setattr(h, "ensure_initialized", _forbidden)
    assert client.get("/symbols").status_code == 409
    assert _cached(str(terminal)) is None

    # Step 2: the documented partial example is stored, but cannot show the
    # broker lacks anything, so the suffix is still appended everywhere.
    resp = client.post(
        "/symbols/import",
        json={"symbols": ["EURUSD", "GBPUSD", "XAUUSD"]},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"imported": 3, "cached": 3, "complete": False}
    assert _tester_symbol("XAUUSD") == "XAUUSD.i"
    assert _tester_symbol("EURUSD") == "EURUSD.i"

    # Step 3: the broker's full book, marked complete, replaces the cache.
    resp = client.post(
        "/symbols/import",
        json={"symbols": ["EURUSD.i", "GBPUSD.i", "XAUUSD"], "complete": True},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"imported": 3, "cached": 3, "complete": True}

    # Step 4: the builder now reads that cache. XAUUSD is carried bare and not
    # suffixed, so it keeps its name; EURUSD is suffix-only and still remaps.
    assert _tester_symbol("XAUUSD") == "XAUUSD"
    assert _tester_symbol("EURUSD") == "EURUSD.i"


def _tester_symbol(symbol):
    import configparser

    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser["Tester"] = {"Symbol": symbol}
    backtest_handler._normalize_symbol(parser)
    return parser["Tester"]["Symbol"]


# ── Per-request bounds ───────────────────────────────────────────────
#
# The endpoint writes caller-supplied text straight into
# <terminal>/mt5api-symbols.json, which the backtest INI builder reads and
# parses on every run inside a fixed-disk Windows VM. Before these caps a
# single authenticated request could hand the JSON parser an unbounded body
# and persist whatever came out: one `symbols` item of 2,097,153 bytes was
# accepted with a 200 and cached. All three caps are configurable through
# mt5api/config.py; the tests below pin the max / max-plus-one boundary of
# each, and the body cap's placement BEFORE request.get_json().


def _body(symbols):
    return json.dumps({"symbols": symbols}).encode("utf-8")


def test_import_symbols_accepts_a_body_exactly_at_the_byte_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    payload = _body(["EURUSD", "XAUUSD"])
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_BODY_BYTES", len(payload))

    resp = _client().post(
        "/symbols/import", data=payload, content_type="application/json"
    )

    assert resp.status_code == 200
    assert resp.get_json()["imported"] == 2
    assert _cached(str(tmp_path)) == {"EURUSD", "XAUUSD"}


def test_import_symbols_rejects_a_body_one_byte_over_the_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    payload = _body(["EURUSD", "XAUUSD"])
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_BODY_BYTES", len(payload) - 1)

    resp = _client().post(
        "/symbols/import", data=payload, content_type="application/json"
    )

    assert resp.status_code == 413
    body = resp.get_json()
    assert body is not None and "SYMBOL_IMPORT_MAX_BODY_BYTES" in body["error"]
    assert _cached(str(tmp_path)) is None


def test_import_symbols_rejects_an_oversized_body_before_parsing_it(
    monkeypatch, tmp_path
):
    """The body cap must be enforced BEFORE request.get_json().

    get_json() reads the whole body into memory and builds an object graph
    from it, so a cap checked afterwards has already paid the cost it exists
    to prevent. This request declares a Content-Length over the cap while
    carrying a body that is not valid JSON at all: only an ordering where the
    length gate runs first can answer 413. Parse first and the silent parser
    returns None, which is the generic 400 -- so a 400 here is the failure
    signal, not a pass.
    """
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_BODY_BYTES", 1024)

    resp = _client().post(
        "/symbols/import",
        data=b"this is not json",
        content_type="application/json",
        environ_overrides={"CONTENT_LENGTH": "1025"},
    )

    assert resp.status_code == 413, (
        f"expected 413, got {resp.status_code} -- the body cap ran after "
        "get_json() instead of before it"
    )
    assert _cached(str(tmp_path)) is None


def test_import_symbols_refuses_a_body_with_no_declared_length(monkeypatch, tmp_path):
    """A chunked body has no length to check, so it cannot be bounded up front
    and is refused outright (411) rather than waved past the cap. A request
    with no body at all also has no Content-Length, but carries no transfer
    encoding either -- it keeps the plain 400 it has always had, asserted by
    test_import_symbols_rejects_no_body_at_all above.
    """
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post(
        "/symbols/import",
        data=_body(["EURUSD"]),
        content_type="application/json",
        headers={"Transfer-Encoding": "chunked"},
    )

    assert resp.status_code == 411
    assert _cached(str(tmp_path)) is None


def test_import_symbols_accepts_the_maximum_entry_count(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_SYMBOLS", 5)

    resp = _client().post(
        "/symbols/import", json={"symbols": [f"SYM{i}" for i in range(5)]}
    )

    assert resp.status_code == 200
    assert resp.get_json()["imported"] == 5


def test_import_symbols_rejects_one_entry_over_the_maximum(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_SYMBOLS", 5)

    resp = _client().post(
        "/symbols/import", json={"symbols": [f"SYM{i}" for i in range(6)]}
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body is not None and "SYMBOL_IMPORT_MAX_SYMBOLS" in body["error"]
    assert _cached(str(tmp_path)) is None


def test_entry_count_is_measured_before_deduplication(monkeypatch, tmp_path):
    """Counted on what the caller sent, not on what survives the dedupe.

    The cap's job is to bound the strip/dedupe/sort pass, which runs over the
    raw array -- so a million repetitions of "EURUSD" has to be refused even
    though it would deduplicate to one entry.
    """
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_SYMBOLS", 5)

    resp = _client().post("/symbols/import", json={"symbols": ["EURUSD"] * 6})

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


def test_import_symbols_accepts_a_symbol_exactly_at_the_length_cap(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_SYMBOL_LENGTH", 8)

    resp = _client().post("/symbols/import", json={"symbols": ["E" * 8]})

    assert resp.status_code == 200
    assert _cached(str(tmp_path)) == {"E" * 8}


def test_import_symbols_rejects_a_symbol_one_character_over_the_cap(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_SYMBOL_LENGTH", 8)

    resp = _client().post("/symbols/import", json={"symbols": ["EURUSD", "E" * 9]})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body is not None and "SYMBOL_IMPORT_MAX_SYMBOL_LENGTH" in body["error"]
    # Nothing is persisted: one bad name rejects the whole import rather than
    # silently caching a partial book, which the INI builder would read as the
    # broker's complete symbol list.
    assert _cached(str(tmp_path)) is None


def test_symbol_length_is_measured_after_whitespace_is_stripped(monkeypatch, tmp_path):
    """The cap applies to the NORMALIZED name -- the form that reaches the
    cache and that the INI builder matches against. A legal name padded with
    whitespace must still be accepted.
    """
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(h, "SYMBOL_IMPORT_MAX_SYMBOL_LENGTH", 8)

    resp = _client().post("/symbols/import", json={"symbols": ["   " + "E" * 8 + "  "]})

    assert resp.status_code == 200
    assert _cached(str(tmp_path)) == {"E" * 8}


def test_the_reported_two_megabyte_symbol_is_refused_at_shipped_defaults(tmp_path, monkeypatch):
    """Verbatim reproduction of the review finding, against the real defaults.

    One `symbols` item of 2,097,153 bytes used to return 200 and land in the
    cache. No monkeypatched limits here on purpose -- this asserts the values
    the server actually ships with are the ones that refuse it.
    """
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={"symbols": ["A" * 2_097_153]})

    assert resp.status_code == 413
    assert _cached(str(tmp_path)) is None


def test_a_symbol_under_the_body_cap_but_over_the_length_cap_is_still_refused(
    tmp_path, monkeypatch
):
    """The second half of that finding: shrink the payload under the 2 MiB body
    cap and the per-symbol cap is what has to catch it. Without it, a 1 KB
    "symbol" would still be cached as a broker symbol name.
    """
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={"symbols": ["A" * 1024]})

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


# ── The knobs themselves ─────────────────────────────────────────────


def test_symbol_import_limits_read_the_environment(monkeypatch):
    monkeypatch.setenv("SYMBOL_IMPORT_MAX_SYMBOLS", "123")

    assert config._positive_int_setting(
        "SYMBOL_IMPORT_MAX_SYMBOLS", "symbol_import_max_symbols", 20000
    ) == 123


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "   ", "1.5"])
def test_an_invalid_limit_falls_back_to_the_default(monkeypatch, bad):
    """Clamping 0 to 1 used to turn a byte cap into a one-byte limit. An
    invalid value now means the default, and startup carries on."""
    monkeypatch.setenv("SYMBOL_IMPORT_MAX_SYMBOLS", bad)

    value = config._positive_int_setting(
        "SYMBOL_IMPORT_MAX_SYMBOLS", "symbol_import_max_symbols", 20000
    )

    assert value == 20000


# ── Partial imports on a suffixed broker ─────────────────────────────


def _suffix(monkeypatch, terminal, suffix=".i"):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(terminal))
    monkeypatch.setattr(backtest_handler, "TERMINAL_DIR", str(terminal))
    monkeypatch.setattr(backtest_handler, "SYMBOL_SUFFIX", suffix)
    monkeypatch.setattr(backtest_handler, "SYMBOL_SUFFIX_CONFIGURED", True)


def test_a_partial_import_does_not_drop_a_suffix_the_broker_needs(monkeypatch, tmp_path):
    """The review reproduction: symbol_suffix ".i", empty cache, then the
    documented example posted. The broker only lists EURUSD.i, so EURUSD
    must still remap to it."""
    _suffix(monkeypatch, tmp_path)
    assert _tester_symbol("EURUSD") == "EURUSD.i"

    resp = _client().post(
        "/symbols/import", json={"symbols": ["EURUSD", "GBPUSD", "XAUUSD"]}
    )

    assert resp.status_code == 200
    assert resp.get_json()["complete"] is False
    assert _tester_symbol("EURUSD") == "EURUSD.i"


def test_a_partial_import_merges_into_a_complete_cache(monkeypatch, tmp_path):
    _suffix(monkeypatch, tmp_path)
    client = _client()
    client.post("/symbols/import", json={"symbols": ["EURUSD.i", "XAUUSD"], "complete": True})

    resp = client.post("/symbols/import", json={"symbols": ["ASX200"]})

    assert resp.get_json() == {"imported": 1, "cached": 3, "complete": True}
    assert _cached(str(tmp_path)) == {"EURUSD.i", "XAUUSD", "ASX200"}
    assert _tester_symbol("EURUSD") == "EURUSD.i"
    assert _tester_symbol("XAUUSD") == "XAUUSD"
    assert _tester_symbol("ASX200") == "ASX200"


def test_a_complete_import_replaces_the_cache(monkeypatch, tmp_path):
    _suffix(monkeypatch, tmp_path)
    client = _client()
    client.post("/symbols/import", json={"symbols": ["OLD"], "complete": True})

    resp = client.post("/symbols/import", json={"symbols": ["EURUSD.i"], "complete": True})

    assert resp.get_json() == {"imported": 1, "cached": 1, "complete": True}
    assert _cached(str(tmp_path)) == {"EURUSD.i"}


def test_a_partial_import_does_not_revive_a_stale_cache(monkeypatch, tmp_path):
    _suffix(monkeypatch, tmp_path)
    symbol_cache.save(str(tmp_path), ["EURUSD.i", "XAUUSD"], complete=True)
    path = symbol_cache.cache_path(str(tmp_path))
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["updated"] = 1
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    resp = _client().post("/symbols/import", json={"symbols": ["GBPUSD.i"]})

    assert resp.get_json() == {"imported": 1, "cached": 1, "complete": False}
    assert _tester_symbol("XAUUSD") == "XAUUSD.i"


def test_a_partial_import_keeps_the_full_book_stamp(monkeypatch, tmp_path):
    """A partial import must not restart the trust window of an old full book,
    or repeated small imports would keep it trusted indefinitely."""
    trust_window_seconds = 3600
    full_book_age_seconds = 3000
    shorter_window_seconds = 2000
    _suffix(monkeypatch, tmp_path)
    monkeypatch.setattr(symbol_cache, "MAX_AGE_SECONDS", trust_window_seconds)
    full_book = {
        "updated": int(time.time()) - full_book_age_seconds,
        "complete": True,
        "symbols": ["EURUSD.i", "XAUUSD"],
    }
    with open(symbol_cache.cache_path(str(tmp_path)), "w", encoding="utf-8") as handle:
        json.dump(full_book, handle)

    resp = _client().post("/symbols/import", json={"symbols": ["GBPUSD.i"]})
    assert resp.get_json() == {"imported": 1, "cached": 3, "complete": True}

    # Past the full book's own window it is stale again, partial import or not,
    # so the suffix is appended as it would be with no cache.
    monkeypatch.setattr(symbol_cache, "MAX_AGE_SECONDS", shorter_window_seconds)
    assert _tester_symbol("XAUUSD") == "XAUUSD.i"


@pytest.mark.parametrize("flag", ["yes", 1, None])
def test_import_symbols_rejects_a_non_boolean_complete(monkeypatch, tmp_path, flag):
    monkeypatch.setattr(h, "TERMINAL_DIR", str(tmp_path))

    resp = _client().post("/symbols/import", json={"symbols": ["EURUSD"], "complete": flag})

    assert resp.status_code == 400
    assert _cached(str(tmp_path)) is None


# ── symbol_cache_max_age ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("604800", 604800), ("3600", 3600), ("7d", 604800), ("12h", 43200), ("90m", 5400)],
)
def test_symbol_cache_max_age_reads_bare_numbers_as_seconds(monkeypatch, raw, expected):
    monkeypatch.setenv("SYMBOL_CACHE_MAX_AGE", raw)

    assert config._symbol_cache_max_age() == expected


@pytest.mark.parametrize("raw", ["0", "-1", "-1d", "abc", "0d"])
def test_an_invalid_symbol_cache_max_age_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("SYMBOL_CACHE_MAX_AGE", raw)

    assert config._symbol_cache_max_age() == 7 * 24 * 3600
