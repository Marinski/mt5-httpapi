"""The global per-request body cap in mt5api/server.py.

POST /symbols/import was reported as accepting a 2 MB symbol name, and fixed
with its own cap. But six other routes parsed an unbounded JSON body the same
way -- POST /orders, PUT /orders/<id>, PUT and DELETE /positions/<id>,
POST /symbols/<symbol>/rates/ta, POST /backtest/build-ini and
/backtest/build-set. A per-endpoint cap only bounds the endpoint someone
remembered; these tests pin the backstop that bounds the ones nobody did,
including routes added after this was written.
"""
from __future__ import annotations

import pytest

from mt5api import config
from mt5api.server import app

# Every route that reads a request body, with a minimal valid content type.
# POST /backtest is multipart and gets the larger cap, so it is tested apart.
JSON_BODY_ROUTES = (
    ("POST", "/orders"),
    ("PUT", "/orders/1"),
    ("PUT", "/positions/1"),
    ("DELETE", "/positions/1"),
    ("POST", "/symbols/EURUSD/rates/ta"),
    ("POST", "/backtest/build-ini"),
    ("POST", "/backtest/build-set"),
)


def _client():
    app.config["TESTING"] = True
    return app.test_client()


def _send(method, path, payload, content_type="application/json"):
    return _client().open(
        path, method=method, data=payload, content_type=content_type
    )


@pytest.mark.parametrize("method,path", JSON_BODY_ROUTES)
def test_every_json_route_refuses_a_body_over_the_cap(method, path):
    """Each of these used to hand request.get_json() whatever arrived."""
    payload = b"x" * (config.MAX_REQUEST_BODY_BYTES + 1)

    resp = _send(method, path, payload)

    assert resp.status_code == 413, f"{method} {path} accepted an oversized body"
    assert "MAX_REQUEST_BODY_BYTES" in resp.get_json()["error"]


@pytest.mark.parametrize("method,path", JSON_BODY_ROUTES)
def test_the_cap_runs_before_the_body_is_parsed(method, path):
    """The payload is over the cap and is NOT valid JSON. Only a gate placed
    before the parser can answer 413; one placed after answers 400 (or 500)
    because parsing fails first. This is what makes the cap worth having --
    refusing after get_json() has already built the object graph pays exactly
    the cost the cap exists to avoid.
    """
    payload = b"{" + b"x" * (config.MAX_REQUEST_BODY_BYTES + 1)

    resp = _send(method, path, payload)

    assert resp.status_code == 413


def test_a_body_exactly_at_the_cap_is_not_refused_by_it():
    """Boundary: the cap must not fire at exactly the limit. This asserts the
    cap does NOT trigger, so it cannot be made red by removing the cap -- its
    job is to pin the off-by-one against a future tightening.
    """
    payload = b"x" * config.MAX_REQUEST_BODY_BYTES

    resp = _send("POST", "/backtest/build-ini", payload)

    assert resp.status_code != 413


def test_multipart_uploads_get_the_larger_cap():
    """POST /backtest carries a compiled .ex5 plus its .set and .ini, so the
    JSON cap would refuse legitimate submissions."""
    assert config.MAX_UPLOAD_BODY_BYTES > config.MAX_REQUEST_BODY_BYTES
    payload = b"x" * (config.MAX_REQUEST_BODY_BYTES + 1)

    resp = _send("POST", "/backtest", payload, content_type="multipart/form-data; boundary=x")

    assert resp.status_code != 413, "a multipart upload was held to the JSON cap"


def test_multipart_over_its_own_cap_is_still_refused():
    payload = b"x" * (config.MAX_UPLOAD_BODY_BYTES + 1)

    resp = _send("POST", "/backtest", payload, content_type="multipart/form-data; boundary=x")

    assert resp.status_code == 413
    assert "MAX_UPLOAD_BODY_BYTES" in resp.get_json()["error"]


def test_the_endpoint_cap_still_wins_where_one_is_tighter():
    """POST /symbols/import declares 2 MiB against the global 4 MiB. The
    endpoint's own message must be what a caller sees, so the tighter bound is
    the one reported rather than being masked by the backstop.
    """
    assert config.SYMBOL_IMPORT_MAX_BODY_BYTES < config.MAX_REQUEST_BODY_BYTES
    payload = b"x" * (config.SYMBOL_IMPORT_MAX_BODY_BYTES + 1)

    resp = _send("POST", "/symbols/import", payload)

    assert resp.status_code == 413
    assert "SYMBOL_IMPORT_MAX_BODY_BYTES" in resp.get_json()["error"]
