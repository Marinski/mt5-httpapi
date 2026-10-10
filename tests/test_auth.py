"""A missing or wrong bearer token answers 401 with a JSON body, on the REST
routes and on the per-terminal /mcp mount alike."""
import json

import pytest

from mt5api import server

TOKEN = "test-api-token"
EXPECTED_BODY = {"error": "unauthorized", "code": "UNAUTHORIZED"}


@pytest.fixture
def secured_client(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", TOKEN)
    server.app.config["TESTING"] = True
    return server.app.test_client()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_a_bad_token_on_rest_answers_json_401(secured_client, headers):
    r = secured_client.get("/account", headers=headers)

    assert r.status_code == 401
    assert r.mimetype == "application/json"
    assert r.get_json() == EXPECTED_BODY


def test_the_right_token_gets_through(secured_client):
    r = secured_client.get("/ping", headers={"Authorization": f"Bearer {TOKEN}"})

    assert r.status_code == 200


def test_the_mcp_gate_answers_the_same_body(monkeypatch):
    pytest.importorskip("a2wsgi")
    from mt5api import main

    monkeypatch.setattr(main, "API_TOKEN", TOKEN)
    gated = main._mcp_auth_gate(lambda environ, start_response: [b"reached"])
    started = {}

    def start_response(status, headers):
        started["status"] = status
        started["headers"] = dict(headers)

    body = b"".join(gated({"HTTP_AUTHORIZATION": "Bearer wrong"}, start_response))

    assert started["status"].startswith("401")
    assert started["headers"]["Content-Type"] == "application/json"
    assert json.loads(body) == EXPECTED_BODY
