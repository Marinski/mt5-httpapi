import os
import time

from flask import Flask, abort, g, jsonify, request
from flask_compress import Compress
from mt5api.backtest import handler as backtest_handler
from mt5api.config import API_TOKEN, COMPILE_API_TOKEN
from mt5api.handlers import (
    account,
    compile as compile_handler,
    history,
    orders,
    positions,
    symbols,
    terminal,
)
from mt5api.logger import log

app = Flask(__name__)


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "-"


@app.before_request
def _start_request():
    g.req_start = time.monotonic()
    g.req_id = os.urandom(4).hex()
    log.info(
        "%s -> %s %s ip=%s ua=%r",
        g.req_id, request.method, request.full_path,
        _client_ip(), request.headers.get("User-Agent", "-"),
    )
    auth = request.headers.get("Authorization", "")

    # /compile carries a SECOND, compile-only credential.
    #
    # A caller that only needs to compile must not hold a token that can also
    # place orders, close positions or restart a terminal. So API_TOKEN
    # is accepted everywhere including here, while COMPILE_API_TOKEN is accepted
    # ONLY on this path - every other route falls through to the original check
    # below, which compares against API_TOKEN alone and therefore rejects it.
    if request.path == "/compile":
        accepted = [f"Bearer {t}" for t in (API_TOKEN, COMPILE_API_TOKEN) if t]
        if accepted and auth not in accepted:
            # JSON rather than Flask's HTML error page: the client is documented
            # to treat a non-JSON body as a broken host.
            return jsonify({"ok": False, "log": "unauthorized"}), 401
        return

    if not API_TOKEN:
        return
    if auth != f"Bearer {API_TOKEN}":
        abort(401)


@app.after_request
def _end_request(response):
    start = getattr(g, "req_start", None)
    elapsed_ms = (time.monotonic() - start) * 1000 if start else -1
    req_id = getattr(g, "req_id", "--------")
    size = response.calculate_content_length()
    if size is None:
        size = response.headers.get("Content-Length", "-")
    log.info(
        "%s <- %s %s status=%s bytes=%s dur_ms=%.1f",
        req_id, request.method, request.full_path,
        response.status_code, size, elapsed_ms,
    )
    return response


# Compress registered AFTER our after_request so its hook runs first
# (Flask invokes after_request hooks in reverse registration order),
# letting us log post-compression Content-Length.
Compress(app)


# ── Health / System ──────────────────────────────────────────────
app.get("/ping")(terminal.ping)
app.get("/error")(terminal.last_error)

# ── Terminal ─────────────────────────────────────────────────────
app.get("/terminal")(terminal.get_terminal)
app.post("/terminal/init")(terminal.init)
app.post("/terminal/shutdown")(terminal.shutdown)
app.post("/terminal/restart")(terminal.restart)

# ── Account ──────────────────────────────────────────────────────
app.get("/account")(account.get_account)

# ── Symbols ──────────────────────────────────────────────────────
app.get("/symbols")(symbols.list_symbols)
app.get("/symbols/<symbol>")(symbols.get_symbol)
app.get("/symbols/<symbol>/tick")(symbols.get_tick)
app.get("/symbols/<symbol>/rates")(symbols.get_rates)
app.post("/symbols/<symbol>/rates/ta")(symbols.get_rates_ta)
app.get("/symbols/<symbol>/ticks")(symbols.get_ticks)

# ── Positions ────────────────────────────────────────────────────
app.get("/positions")(positions.list_positions)
app.get("/positions/<int:ticket>")(positions.get_position)
app.put("/positions/<int:ticket>")(positions.update_position)
app.delete("/positions/<int:ticket>")(positions.close_position)

# ── Orders ───────────────────────────────────────────────────────
app.get("/orders")(orders.list_orders)
app.post("/orders")(orders.create_order)
app.get("/orders/<int:ticket>")(orders.get_order)
app.put("/orders/<int:ticket>")(orders.update_order)
app.delete("/orders/<int:ticket>")(orders.cancel_order)

# ── History ──────────────────────────────────────────────────────
app.get("/history/orders")(history.get_orders)
app.get("/history/deals")(history.get_deals)

# ── Compile ──────────────────────────────────────────────────────
# Source text in, .ex5 out. Auth for this one route is handled in
# _start_request above; it accepts COMPILE_API_TOKEN as well as API_TOKEN.
app.post("/compile")(compile_handler.compile_source)

# ── Backtest ─────────────────────────────────────────────────────
app.post("/backtest/build-ini")(backtest_handler.build_ini_route)
app.post("/backtest/build-set")(backtest_handler.build_set_route)
app.post("/backtest")(backtest_handler.run_backtest)
app.get("/backtest/<job_id>")(backtest_handler.get_status)
app.get("/backtest/<job_id>/report")(backtest_handler.get_report)
app.get("/backtest/<job_id>/log")(backtest_handler.get_log)
app.get("/backtest/<job_id>/tail")(backtest_handler.get_tail)
