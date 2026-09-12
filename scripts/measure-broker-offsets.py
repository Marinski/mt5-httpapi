#!/usr/bin/env python3
"""Measure each terminal's broker-clock offset from real UTC.

config.yaml's per-terminal `utc_offset` is a STATIC number that mt5api
subtracts from every broker timestamp (mt5client.broker_to_utc_seconds).
Nothing in the stack is DST-aware, so a value that is right in August is
one hour wrong after the autumn rollover. Re-run this then and update
config.yaml with whatever it prints.

Reads the latest tick and compares its broker timestamp to local UTC, so
it only works while the market is open and the terminal can log in.

WARNING: hitting an SDK route on a `mode: backtest` terminal makes mt5api
launch terminal64.exe, and POST /terminal/shutdown only detaches the SDK
client — it does not close the terminal. A terminal left running holds
MT5's single-instance lock on the data dir, and the next backtest there
spawns a second terminal64.exe that exits silently with code 0, producing
an empty "Bars=0 Ticks=0 Symbols=0" report. Run this only when you can
follow it with `docker compose down && ./run.sh`.

Usage:
    python3 scripts/measure-broker-offsets.py [broker/account ...]

With no arguments it measures every terminal in config.yaml.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, os.pardir, "config", "config.yaml")
BASE = os.environ.get("MT5_HTTPAPI_BASE_URL", "http://127.0.0.1:8888")
TIMEOUT = int(os.environ.get("MEASURE_TIMEOUT", "120"))
# Broker clocks sit on quarter-hour boundaries; snapping absorbs tick latency.
QUANTUM = 900
PREFERRED = ("EURUSD", "BTCUSD", "XAUUSD")


def load_config():
    with open(CONFIG, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def api(token, path):
    req = urllib.request.Request(
        f"{BASE}/{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def pick_symbol(token, route):
    """Shortest name matching a liquid, near-24h instrument."""
    symbols = api(token, f"{route}/symbols")
    if not isinstance(symbols, list):
        raise RuntimeError(f"unexpected /symbols payload: {symbols!r}")
    for prefix in PREFERRED:
        matches = [s for s in symbols if s.upper().startswith(prefix)]
        if matches:
            return sorted(matches, key=len)[0]
    raise RuntimeError("no EURUSD/BTCUSD/XAUUSD variant in the symbol list")


def measure(token, route):
    symbol = pick_symbol(token, route)
    before = time.time()
    tick = api(token, f"{route}/symbols/{symbol}/tick")
    after = time.time()
    broker_time = tick.get("time")
    if not broker_time:
        raise RuntimeError(f"tick carried no time: {tick!r}")
    # The route already subtracts the CONFIGURED offset, so add it back to
    # recover the raw broker clock — otherwise this reports 0 once the config
    # is correct instead of confirming it.
    configured = api(token, f"{route}/terminal").get("broker_utc_offset_seconds", 0)
    raw = (broker_time + configured) - (before + after) / 2
    offset = round(raw / QUANTUM) * QUANTUM
    return symbol, offset, raw, configured


def main():
    cfg = load_config()
    token = cfg.get("api_token") or ""
    routes = sys.argv[1:]
    if not routes:
        seen, routes = set(), []
        for term in cfg.get("terminals", []):
            route = f"{term['broker']}/{term['account']}"
            if route not in seen:
                seen.add(route)
                routes.append(route)

    failures = 0
    print(f"{'terminal':<32}{'symbol':<14}{'measured':>10}{'configured':>12}")
    for route in routes:
        try:
            symbol, offset, raw, configured = measure(token, route)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            failures += 1
            print(f"{route:<32}FAILED: {exc}")
            continue
        flag = "" if offset == configured else "   <-- MISMATCH"
        print(
            f"{route:<32}{symbol:<14}{offset / 3600:>+9.2f}h"
            f"{configured / 3600:>+11.2f}h{flag}  (raw {raw:.1f}s)"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
