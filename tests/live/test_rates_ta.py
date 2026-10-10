"""Wickworks TA passthrough: POST /symbols/<symbol>/rates/ta with an
indicators spec returns bars + computed TA.

Wickworks is primitives-only (rsi/ema/sma/atr/macd/bbands/stoch/adx/...)
with no divergences, signals, or divTrends. If wickworks cannot be reached the
endpoint returns 502 with no wickworks status; we skip rather than fail in that
case. A request wickworks answered and rejected is a failure.
"""
from __future__ import annotations

import pytest

import requests

_HTTP_BAD_GATEWAY = 502


def _skip_if_wickworks_unreachable(resp):
    if resp.status_code != _HTTP_BAD_GATEWAY:
        return
    if "wickworks_status" in resp.json():
        return
    pytest.skip(f"wickworks unavailable: {resp.text[:200]}")


def _post_ta(client, symbol, indicators, **params):
    base = client.base_url.rstrip("/")
    resp = requests.post(
        f"{base}/symbols/{symbol}/rates/ta",
        params=params or None,
        json={"indicators": indicators},
        headers={"Authorization": f"Bearer {client.token}"},
        timeout=30,
    )
    return resp


def test_rates_ta_rsi_ema_atr(client, config):
    resp = _post_ta(
        client,
        config["symbol"],
        indicators={
            "rsi14": {"type": "rsi", "length": 14},
            "ema21": {"type": "ema", "length": 21},
            "atr14": {"type": "atr", "length": 14},
        },
        timeframe="H1",
        count=200,
    )
    _skip_if_wickworks_unreachable(resp)
    assert resp.status_code == 200, f"unexpected status: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body["symbol"] == config["symbol"]
    assert body["timeframe"] == "H1"
    assert isinstance(body["bars"], list) and len(body["bars"]) > 0
    assert body["ta"] is not None, f"ta is null: {body}"
    # Wickworks puts results under the keys we supplied.
    ta = body["ta"]
    for key in ("rsi14", "ema21", "atr14"):
        assert key in ta, f"{key} missing from ta: {list(ta.keys())[:10]}"


def test_rates_ta_macd(client, config):
    resp = _post_ta(
        client,
        config["symbol"],
        indicators={"macd": {"fast": 12, "slow": 26, "signal": 9}},
        timeframe="H1",
        count=200,
    )
    _skip_if_wickworks_unreachable(resp)
    assert resp.status_code == 200, f"status {resp.status_code}: {resp.text[:300]}"
    assert "macd" in resp.json()["ta"], f"macd missing from ta: {resp.text[:300]}"


def test_rates_ta_empty_indicators_returns_400(client, config):
    base = client.base_url.rstrip("/")
    resp = requests.post(
        f"{base}/symbols/{config['symbol']}/rates/ta",
        params={"timeframe": "H1", "count": 50},
        json={"indicators": {}},
        headers={"Authorization": f"Bearer {client.token}"},
        timeout=15,
    )
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text[:200]}"


def test_rates_ta_missing_body_returns_400(client, config):
    base = client.base_url.rstrip("/")
    resp = requests.post(
        f"{base}/symbols/{config['symbol']}/rates/ta",
        params={"timeframe": "H1", "count": 50},
        headers={"Authorization": f"Bearer {client.token}"},
        timeout=15,
    )
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text[:200]}"
