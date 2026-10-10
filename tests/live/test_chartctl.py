"""Chart Deployments end to end on the running stack, through the unified MCP
endpoint: compile the probe expert, stage it, deploy it, look at the chart,
switch its set file, and delete it again.

Needs chartctl on the target terminal and, unless MT5_LIVE_ALLOW_REAL=1, a
demo account (see conftest). The probe never trades.
"""
from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path

import pytest

from tests.live import png
from tests.live.conftest import ARTIFACT_PREFIX
from tests.live.live_client import LiveAPIError, McpClient, RestClient

PROBE_SOURCE = Path(__file__).parent / "fixtures" / "ChartctlProbe.mq5"
# MQL5 colours are 0x00BBGGRR: 65280 is pure green, 255 pure red. Nothing on
# a default chart is pure red, so red pixels mean the set-B label is drawn.
SET_A = "ProbeLabel=set-A\nLabelColor=65280\nTimerSeconds=2\n"
SET_B = "ProbeLabel=set-B\nLabelColor=255\nTimerSeconds=2\n"
PURE_RED = (255, 0, 0)
NAVIGATOR_REFRESH_OK = "ok"
RUNNING = "running"
ATTACH_TIMEOUT_SECONDS = 150
CHART_CLOSE_TIMEOUT_SECONDS = 120
OWNERSHIP_WATCH_SECONDS = 20
POLL_SECONDS = 3
EXPERT_REDRAW_SECONDS = 6
_HTTP_SERVICE_UNAVAILABLE = 503


def _wait(predicate, what: str, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _charts(mcp: McpClient, symbol: str, timeframe: str) -> list[dict]:
    period = f"PERIOD_{timeframe}"
    return [
        chart
        for chart in mcp.call_json("list_charts")["charts"]
        if chart["symbol"] == symbol and chart["timeframe"] == period
    ]


def _deployment(mcp: McpClient, deployment_id: str) -> dict:
    return mcp.call_json("get_deployment", deployment_id=deployment_id)


def _chart_id(dep: dict) -> int:
    return (dep.get("observed") or {}).get("chart_id")


def _wait_status(mcp: McpClient, deployment_id: str, wanted: str) -> dict:
    def reached():
        dep = _deployment(mcp, deployment_id)
        if dep["status"] in ("failed", "degraded"):
            raise AssertionError(f"deployment {dep['status']}: {dep.get('error')}")
        return dep if dep["status"] == wanted else None

    return _wait(reached, f"deployment {wanted}", ATTACH_TIMEOUT_SECONDS)


def _red_pixels(mcp: McpClient, chart_id: int) -> int:
    (block,) = mcp.call("screenshot_chart", chart_id=chart_id)
    assert block["type"] == "image" and block["mimeType"] == "image/png"
    _, _, pixels = png.decode(base64.b64decode(block["data"]))
    return png.count_color(pixels, PURE_RED)


@pytest.fixture(scope="module")
def probe(chartctl: McpClient, rest: RestClient) -> dict:
    """Compile the probe on the target and stage it under a fresh name, so the
    terminal has never seen it: that is the case the Navigator refresh covers."""
    try:
        compiled = rest.post(
            "/compile",
            json={"source": PROBE_SOURCE.read_text(encoding="utf-8"), "filename": "probe.mq5"},
            timeout=200,
        )
    except LiveAPIError as err:
        if f"-> {_HTTP_SERVICE_UNAVAILABLE}" in str(err) or "not available" in str(err):
            pytest.skip(f"POST /compile is not available on the target: {err}")
        raise
    assert compiled["ok"], compiled.get("log", "")[-1500:]

    name = f"{ARTIFACT_PREFIX}probe-{uuid.uuid4().hex[:8]}.ex5"
    upload = chartctl.call_json(
        "upload_expert",
        filename=name,
        content_base64=compiled["ex5_base64"],
    )
    sets = {
        label: chartctl.call_json(
            "upload_set",
            filename=f"{ARTIFACT_PREFIX}{label}.set",
            content=text,
        )
        for label, text in (("a", SET_A), ("b", SET_B))
    }
    return {"name": name, "upload": upload, "sets": sets}


@pytest.fixture(scope="module")
def deployment(chartctl: McpClient, probe: dict, settings: dict) -> dict:
    if probe["upload"].get("navigator_refresh") != NAVIGATOR_REFRESH_OK:
        pytest.skip(f"new experts are not loadable without a restart here: {probe['upload']}")
    symbol, timeframe = settings["symbol"], settings["timeframe"]
    baseline = len(_charts(chartctl, symbol, timeframe))
    created = chartctl.call_json(
        "create_deployment",
        expert=probe["name"],
        symbol=symbol,
        timeframe=timeframe,
        set_file=f"{ARTIFACT_PREFIX}a.set",
    )
    _wait_status(chartctl, created["id"], RUNNING)
    return {"id": created["id"], "symbol": symbol, "timeframe": timeframe, "baseline": baseline}


def test_upload_refreshes_the_navigator(probe):
    assert probe["upload"]["navigator_refresh"] == NAVIGATOR_REFRESH_OK, probe["upload"]


def test_set_files_are_parsed(probe):
    assert {"name": "ProbeLabel", "value": "set-A"} in probe["sets"]["a"]["inputs"]
    assert {"name": "LabelColor", "value": "255"} in probe["sets"]["b"]["inputs"]


def test_the_deployment_runs_on_exactly_one_new_chart(chartctl, deployment):
    charts = _charts(chartctl, deployment["symbol"], deployment["timeframe"])

    owned = [c for c in charts if c["deployment_id"] == deployment["id"]]
    assert len(charts) == deployment["baseline"] + 1
    assert len(owned) == 1 and owned[0]["expert"]


def test_the_chart_screenshot_is_a_png_of_set_a(chartctl, deployment):
    dep = _deployment(chartctl, deployment["id"])

    assert _red_pixels(chartctl, _chart_id(dep)) == 0


def test_an_expert_writing_the_chart_comment_keeps_its_owner(chartctl, deployment):
    """The probe rewrites the chart comment every 2 seconds; loader 1.0.2
    marked ownership there and lost the chart."""
    first = _chart_id(_deployment(chartctl, deployment["id"]))
    deadline = time.time() + OWNERSHIP_WATCH_SECONDS
    while time.time() < deadline:
        dep = _deployment(chartctl, deployment["id"])
        assert dep["status"] == RUNNING
        assert _chart_id(dep) == first
        charts = _charts(chartctl, deployment["symbol"], deployment["timeframe"])
        assert len(charts) == deployment["baseline"] + 1
        time.sleep(POLL_SECONDS)


def test_a_new_set_reaches_the_expert_only_after_pause_and_resume(chartctl, deployment):
    dep_id = deployment["id"]
    chartctl.call_json(
        "update_deployment",
        deployment_id=dep_id,
        set_file=f"{ARTIFACT_PREFIX}b.set",
    )
    time.sleep(EXPERT_REDRAW_SECONDS)
    assert _red_pixels(chartctl, _chart_id(_deployment(chartctl, dep_id))) == 0

    chartctl.call_json("update_deployment", deployment_id=dep_id, enabled=False)
    _wait(
        lambda: len(_charts(chartctl, deployment["symbol"], deployment["timeframe"]))
        == deployment["baseline"],
        "the paused deployment's chart to close",
        CHART_CLOSE_TIMEOUT_SECONDS,
    )
    chartctl.call_json("update_deployment", deployment_id=dep_id, enabled=True)
    dep = _wait_status(chartctl, dep_id, RUNNING)
    time.sleep(EXPERT_REDRAW_SECONDS)

    assert _red_pixels(chartctl, _chart_id(dep)) > 0


def test_the_terminal_endpoint_sees_the_same_loader(mcp_terminal, chartctl):
    unified = chartctl.call_json("get_loader")
    per_terminal = mcp_terminal.call_json("get_loader")["body"]

    assert unified["alive"] is per_terminal["alive"] is True
    assert unified["loader"]["version"] == per_terminal["loader"]["version"]


def test_delete_closes_the_chart_and_forgets_the_deployment(chartctl, deployment, probe):
    chartctl.call_json("delete_deployment", deployment_id=deployment["id"])

    _wait(
        lambda: len(_charts(chartctl, deployment["symbol"], deployment["timeframe"]))
        == deployment["baseline"],
        "the deleted deployment's chart to close",
        CHART_CLOSE_TIMEOUT_SECONDS,
    )
    with pytest.raises(LiveAPIError, match="NOT_FOUND"):
        _deployment(chartctl, deployment["id"])
    chartctl.call_json("delete_expert", name=probe["name"])


def test_upload_rejects_malformed_base64(chartctl):
    with pytest.raises(LiveAPIError, match="not valid base64"):
        chartctl.call_json(
            "upload_expert", filename=f"{ARTIFACT_PREFIX}bad.ex5", content_base64="not base64!",
        )
