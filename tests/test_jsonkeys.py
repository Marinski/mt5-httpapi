"""snake_case response keys with the deprecated camelCase ones alongside, and
request bodies that accept either form."""
import pytest

from mt5api.jsonkeys import ConflictingKeys, accept_snake_keys, to_snake, with_legacy_keys


@pytest.mark.parametrize(
    ("camel", "snake"),
    [
        ("jobId", "job_id"),
        ("maxDrawdownAbsolute", "max_drawdown_absolute"),
        ("latencyMs", "latency_ms"),
        ("status", "status"),
        ("time_msc", "time_msc"),
        ("ex5Base64", "ex5_base64"),
    ],
)
def test_to_snake(camel, snake):
    assert to_snake(camel) == snake


def test_legacy_keys_ride_alongside_and_snake_keys_are_untouched():
    out = with_legacy_keys({"jobId": "x", "status": "ok", "time_msc": 1})

    assert out == {"job_id": "x", "jobId": "x", "status": "ok", "time_msc": 1}


def test_legacy_conversion_does_not_reach_into_nested_data():
    out = with_legacy_keys({"expertInputs": {"TakeProfitPips": 30}})

    assert out["expert_inputs"] == {"TakeProfitPips": 30}


def test_snake_input_is_renamed_to_the_field_the_handler_reads():
    body = accept_snake_keys({"from_date": "2026-01-01", "symbol": "EURUSD"}, ["fromDate"])

    assert body == {"fromDate": "2026-01-01", "symbol": "EURUSD"}


def test_both_forms_with_the_same_value_are_fine():
    body = accept_snake_keys({"from_date": "2026-01-01", "fromDate": "2026-01-01"}, ["fromDate"])

    assert body == {"fromDate": "2026-01-01"}


def test_both_forms_with_different_values_are_refused():
    with pytest.raises(ConflictingKeys, match="from_date and fromDate"):
        accept_snake_keys({"from_date": "2026-01-01", "fromDate": "2025-01-01"}, ["fromDate"])


# ── through the routes ───────────────────────────────────────────────

def test_build_ini_takes_snake_case_fields(api_client):
    body = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "expert": "EA.ex5",
        "from_date": "2026-01-01",
        "to_date": "2026-02-01",
        "latency_ms": 7,
        "report_name": "snake-report",
    }

    r = api_client.post("/backtest/build-ini", json=body)

    assert r.status_code == 200, r.data
    ini = r.get_data(as_text=True)
    assert "FromDate=2026.01.01" in ini
    assert "ToDate=2026.02.01" in ini
    assert "snake-report" in ini


def test_build_ini_still_takes_the_camel_case_fields(api_client):
    body = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "expert": "EA.ex5",
        "fromDate": "2026-01-01",
        "toDate": "2026-02-01",
    }

    r = api_client.post("/backtest/build-ini", json=body)

    assert r.status_code == 200
    assert "FromDate=2026.01.01" in r.get_data(as_text=True)


def test_build_ini_refuses_conflicting_forms(api_client):
    body = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "expert": "EA.ex5",
        "from_date": "2026-01-01",
        "fromDate": "2025-01-01",
        "to_date": "2026-02-01",
    }

    r = api_client.post("/backtest/build-ini", json=body)

    assert r.status_code == 400
    assert "from_date and fromDate" in r.get_json()["error"]


def test_a_wickworks_rejection_reports_snake_case_and_legacy_keys(
    api_client, patch_handler, monkeypatch
):
    from mt5api.handlers import symbols
    from tests.test_live_api_contract import _fake_bars

    recorder = patch_handler(symbols)
    recorder.set("copy_rates_from_pos", _fake_bars(10))
    monkeypatch.setattr(
        symbols, "_call_wickworks", lambda payload: ({"detail": "bad"}, 400, None)
    )

    r = api_client.post(
        "/symbols/EURUSD/rates/ta?timeframe=H1&count=10",
        json={"indicators": {"rsi": True}},
    )

    assert r.status_code == 502
    body = r.get_json()
    assert body["wickworks_status"] == body["wickworksStatus"] == 400
    assert body["wickworks_body"] == body["wickworksBody"] == {"detail": "bad"}


def test_top_passes_is_read_from_either_form_field():
    from flask import Flask

    from mt5api.backtest import handler

    app = Flask("form_field")
    for form, expected in (
        ({"top_passes": "7"}, "7"),
        ({"topPasses": "8"}, "8"),
        ({"top_passes": "9", "topPasses": "9"}, "9"),
        ({}, None),
    ):
        with app.test_request_context(method="POST", data=form):
            assert handler._form_field("top_passes", "topPasses") == expected
    with app.test_request_context(method="POST", data={"top_passes": "1", "topPasses": "2"}):
        with pytest.raises(ValueError, match="same field"):
            handler._form_field("top_passes", "topPasses")
