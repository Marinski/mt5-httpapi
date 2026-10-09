"""Read-only REST checks against the target terminal."""
from __future__ import annotations

import time

RATES_COUNT = 10
HISTORY_WINDOW_SECONDS = 7 * 24 * 3600


def test_ping_reports_the_live_process(rest):
    pong = rest.get("/ping")

    assert pong["status"] == "ok"
    assert pong["mode"] == "live"


def test_terminal_is_connected(rest):
    info = rest.get("/terminal")

    assert info["connected"] is True


def test_account_has_identity_and_balance(account):
    assert account["login"] > 0
    assert account["currency"]
    assert "balance" in account and "equity" in account


def test_the_test_symbol_is_listed_and_quoted(rest, settings):
    symbol = settings["symbol"]

    assert symbol in rest.get("/symbols", params={"group": f"*{symbol}*"})
    spec = rest.get(f"/symbols/{symbol}")
    assert spec["name"] == symbol
    tick = rest.get(f"/symbols/{symbol}/tick")
    assert tick["bid"] > 0 and tick["ask"] >= tick["bid"]


def test_rates_return_the_requested_bars(rest, settings):
    bars = rest.get(
        f"/symbols/{settings['symbol']}/rates",
        params={"timeframe": "M1", "count": RATES_COUNT},
    )

    assert len(bars) == RATES_COUNT
    assert all(bar["low"] <= bar["open"] <= bar["high"] for bar in bars)
    assert [bar["time"] for bar in bars] == sorted(bar["time"] for bar in bars)


def test_history_answers_a_list(rest):
    now = int(time.time())
    window = {"from": now - HISTORY_WINDOW_SECONDS, "to": now}

    assert isinstance(rest.get("/history/deals", params=window), list)
    assert isinstance(rest.get("/history/orders", params=window), list)
