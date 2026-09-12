import os
import time

from flask import Flask, abort, g, jsonify, request
from flask_compress import Compress
from mt5api.backtest import handler as backtest_handler
from mt5api.config import (
    API_TOKEN,
    MAX_REQUEST_BODY_BYTES,
    MAX_UPLOAD_BODY_BYTES,
)
from mt5api.handlers import account, history, orders, positions, symbols, terminal
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
    if API_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_TOKEN}":
            abort(401)
    return _refuse_oversized_body()


def _refuse_oversized_body():
    """Bound the request body before any handler parses it.

    Deliberately here and not in each handler. A per-endpoint cap has to be
    remembered on every route added afterwards, and the one that gets forgotten
    is the hole — which is how POST /symbols/import came to accept a 2 MB
    symbol name while six other JSON routes had no bound at all. An endpoint
    that needs a tighter cap still declares one and it fires first, because it
    is checked inside the handler; this only catches what nothing else bounded.

    Content-Length is what makes this a PRE-PARSE gate: it is the one thing
    known before a byte is read. A body with no declared length is left to the
    endpoint (POST /symbols/import refuses it outright) and, in production, to
    waitress, which de-chunks and supplies a length before Flask sees the
    request at all.

    Runs after the auth check so an unauthenticated caller cannot probe the
    limits, and answers JSON because the client treats a non-JSON body as a
    broken host.
    """
    declared = request.content_length
    if declared is None:
        return None
    multipart = (request.mimetype or "").startswith("multipart/")
    limit = MAX_UPLOAD_BODY_BYTES if multipart else MAX_REQUEST_BODY_BYTES
    if declared <= limit:
        return None
    setting = "MAX_UPLOAD_BODY_BYTES" if multipart else "MAX_REQUEST_BODY_BYTES"
    log.warning(
        "%s rejected %d-byte body on %s %s (cap %d)",
        getattr(g, "req_id", "--------"), declared,
        request.method, request.path, limit,
    )
    return jsonify({
        "error": (
            f"request body is {declared} bytes; this server accepts at most "
            f"{limit} ({setting})"
        ),
    }), 413


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
app.post("/symbols/import")(symbols.import_symbols)
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

# ── Backtest ─────────────────────────────────────────────────────
app.post("/backtest/build-ini")(backtest_handler.build_ini_route)
app.post("/backtest/build-set")(backtest_handler.build_set_route)
app.post("/backtest")(backtest_handler.run_backtest)
app.get("/backtest/<job_id>")(backtest_handler.get_status)
app.get("/backtest/<job_id>/report")(backtest_handler.get_report)
app.get("/backtest/<job_id>/log")(backtest_handler.get_log)
app.get("/backtest/<job_id>/tail")(backtest_handler.get_tail)
