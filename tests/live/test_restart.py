"""A terminal restart through the API, end to end, on the running stack.

Opt-in with MT5_LIVE_RESTART=1: it restarts the target terminal, which takes
it offline for a minute or two. Needs Chart Deployments and the file API on
the target and a demo account unless MT5_LIVE_ALLOW_REAL=1.

The probe expert holds a file open without sharing and calls WebRequest() on
a URL the suite adds to the allowlist, writing the result to MQL5/Files.
Before the restart that proves the allowlist is live and gives the file API a
file Windows will not let anyone else touch. After POST /terminal/restart:

- the deployment is running again on exactly one chart (the loader adopted
  the chart MT5 restored instead of opening a second one);
- the re-launched probe holds its file again;
- the re-launched probe's WebRequest() succeeds, which only happens if the
  API re-applied the allowlist the restarted terminal forgot.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from tests.live.conftest import ARTIFACT_PREFIX, flag
from tests.live.live_client import LiveAPIError, McpClient, RestClient

PROBE_SOURCE = Path(__file__).parent / "fixtures" / "ChartctlProbe.mq5"
DEFAULT_WEBREQUEST_URL = "https://example.com/"
RUNNING = "running"
HTTP_OK_CODE = "200"
_HTTP_CONFLICT = 409
_HTTP_NOT_FOUND = 404
ATTACH_TIMEOUT_SECONDS = 180
WEBREQUEST_TIMEOUT_SECONDS = 300
RESTART_REQUEST_TIMEOUT_SECONDS = 420
LOADER_BACK_TIMEOUT_SECONDS = 300
POLL_SECONDS = 5
TIMER_SECONDS = 5
# Longer than the loader's 30 s startup grace, so a duplicate chart opened
# when the grace ends (beside a restored chart that is still loading, or one
# MT5 restored without its expert) has appeared before the charts are counted.
DUPLICATE_SETTLE_SECONDS = 45

pytestmark = pytest.mark.skipif(
    not flag("MT5_LIVE_RESTART"),
    reason="restarts the target terminal; set MT5_LIVE_RESTART=1 to run",
)


def _wait(predicate, what: str, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _status(rest: RestClient, path: str) -> dict | None:
    """The probe's WebRequest report, or None while it is not there yet."""
    resp = rest.session.get(f"{rest.base}/files/{path}", timeout=60)
    if resp.status_code != 200:
        return None
    pairs = (line.split("=", 1) for line in resp.text.splitlines() if "=" in line)
    return {key.strip(): value.strip() for key, value in pairs}


def _deployment(mcp: McpClient, deployment_id: str) -> dict:
    return mcp.call_json("get_deployment", deployment_id=deployment_id)


def _pair_charts(mcp: McpClient, symbol: str, timeframe: str) -> list[dict]:
    """The loader reports timeframes as MQL5 enum names (PERIOD_M15)."""
    period = f"PERIOD_{timeframe}"
    charts = mcp.call_json("list_charts")["charts"]
    return [c for c in charts if c["symbol"] == symbol and c["timeframe"] == period]


def _owned_charts(mcp: McpClient, symbol: str, timeframe: str, deployment_id: str) -> list[dict]:
    charts = _pair_charts(mcp, symbol, timeframe)
    return [c for c in charts if c.get("deployment_id") == deployment_id]


def _wait_running(mcp: McpClient, deployment_id: str) -> dict:
    def running():
        dep = _deployment(mcp, deployment_id)
        if dep["status"] in ("failed", "degraded"):
            raise AssertionError(f"deployment {dep['status']}: {dep.get('error')}")
        return dep if dep["status"] == RUNNING else None

    return _wait(running, "deployment running", ATTACH_TIMEOUT_SECONDS)


@pytest.fixture(scope="module")
def restart_probe(chartctl: McpClient, files_api: McpClient, rest: RestClient, settings: dict):
    tag = f"{ARTIFACT_PREFIX}restart-{uuid.uuid4().hex[:8]}"
    files_dir = f"MQL5/Files/{tag}"
    url = os.environ.get("MT5_LIVE_WEBREQUEST_URL") or DEFAULT_WEBREQUEST_URL
    compiled = rest.post(
        "/compile",
        json={"source": PROBE_SOURCE.read_text(encoding="utf-8"), "filename": "probe.mq5"},
        timeout=200,
    )
    assert compiled["ok"], compiled.get("log", "")[-1500:]
    expert = f"{tag}.ex5"
    upload = chartctl.call_json(
        "upload_expert", filename=expert, content_base64=compiled["ex5_base64"],
    )
    if upload.get("navigator_refresh") != "ok":
        pytest.skip(f"new experts are not loadable without a restart here: {upload}")
    set_name = f"{tag}.set"
    chartctl.call_json(
        "upload_set",
        filename=set_name,
        content=(
            f"ProbeLabel=restart\nTimerSeconds={TIMER_SECONDS}\n"
            f"LockFile={tag}\\locked.bin\nWebRequestUrl={url}\n"
            f"StatusFile={tag}\\webrequest.txt\n"
        ),
    )
    chartctl.call_json("set_webrequest", add=[url])
    symbol, timeframe = settings["symbol"], settings["timeframe"]
    created = chartctl.call_json(
        "create_deployment",
        expert=expert,
        symbol=symbol,
        timeframe=timeframe,
        set_file=set_name,
    )
    _wait_running(chartctl, created["id"])
    probe = {
        "deployment_id": created["id"],
        "symbol": symbol,
        "timeframe": timeframe,
        "lock_path": f"{files_dir}/locked.bin",
        "status_path": f"{files_dir}/webrequest.txt",
        "url": url,
    }
    yield probe

    chartctl.call_json("delete_deployment", deployment_id=created["id"])
    _wait(
        lambda: not _owned_charts(chartctl, symbol, timeframe, created["id"]),
        "the probe chart to close",
        ATTACH_TIMEOUT_SECONDS,
    )
    chartctl.call_json("set_webrequest", remove=[url])
    try:
        files_api.call_json("delete_file", path=files_dir, recursive=True)
    except LiveAPIError as err:
        pytest.fail(f"could not remove {files_dir}: {err}")


def _wait_webrequest_ok(rest: RestClient, path: str, after_started: str | None = None) -> dict:
    def ok():
        status = _status(rest, path)
        if not status or status.get("code") != HTTP_OK_CODE:
            return None
        if after_started is not None and status.get("started") == after_started:
            return None
        return status

    what = "a successful WebRequest() from the probe"
    return _wait(ok, what, WEBREQUEST_TIMEOUT_SECONDS)


def test_the_allowlist_lets_the_probe_call_webrequest(rest, restart_probe):
    status = _wait_webrequest_ok(rest, restart_probe["status_path"])

    assert status["error"] == "0"


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("put", b"replace"), ("delete", None)],
)
def test_a_file_the_expert_holds_open_is_reported_locked(rest, restart_probe, method, body):
    url = f"{rest.base}/files/{restart_probe['lock_path']}"
    resp = rest.session.request(method.upper(), url, data=body, timeout=60)

    assert resp.status_code == _HTTP_CONFLICT, resp.text[:300]
    assert resp.json()["code"] == "FILE_LOCKED"


def test_a_restart_keeps_one_chart_and_reapplies_the_allowlist(chartctl, rest, restart_probe):
    symbol, timeframe = restart_probe["symbol"], restart_probe["timeframe"]
    before = _status(rest, restart_probe["status_path"])
    assert before, "the probe has not reported yet"
    charts_before = len(_pair_charts(chartctl, symbol, timeframe))

    restarted = rest.session.post(
        f"{rest.base}/terminal/restart", timeout=RESTART_REQUEST_TIMEOUT_SECONDS,
    )
    assert restarted.status_code == 200, restarted.text[:300]

    _wait(
        lambda: chartctl.call_json("get_loader").get("alive"),
        "the loader to come back",
        LOADER_BACK_TIMEOUT_SECONDS,
    )
    dep = _wait_running(chartctl, restart_probe["deployment_id"])
    time.sleep(DUPLICATE_SETTLE_SECONDS)
    pair_charts = _pair_charts(chartctl, symbol, timeframe)
    assert len(pair_charts) == charts_before, pair_charts
    owned = _owned_charts(chartctl, symbol, timeframe, restart_probe["deployment_id"])
    assert len(owned) == 1, pair_charts
    assert owned[0]["expert"], pair_charts
    assert _deployment(chartctl, restart_probe["deployment_id"])["status"] == RUNNING, dep

    after = _wait_webrequest_ok(rest, restart_probe["status_path"], after_started=before["started"])
    assert after["error"] == "0"

    held = rest.session.get(f"{rest.base}/files/{restart_probe['lock_path']}", timeout=60)
    assert held.status_code == _HTTP_CONFLICT, "the re-launched probe does not hold its file"
