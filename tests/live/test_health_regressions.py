"""Read-only checks for deployed healthcheck logging and native BLAS caps."""
from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests

PING_TIMEOUT_SECONDS = 3  # same deadline as scripts/check_health.py
PROBES_PER_TERMINAL = 5
MAX_PROBE_WORKERS = 16
LOG_WAIT_SECONDS = 5
LOG_POLL_SECONDS = 0.05
BLAS_MARKER = "BLAS_RUNTIME "
START_MARKER = "=== PROCESS STARTED"
THREAD_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")


@pytest.fixture(scope="module")
def terminals(mcp_unified):
    entries = mcp_unified.call_json("list_terminals", routed=False)["terminals"]
    assert entries, "unifier returned no terminals"
    return entries


@pytest.fixture(scope="module")
def log_dir():
    value = os.environ.get("MT5_LIVE_LOG_DIR")
    if not value:
        pytest.skip("set MT5_LIVE_LOG_DIR to the deployed data/shared/logs directory")
    path = Path(value)
    assert path.is_dir(), "MT5_LIVE_LOG_DIR is not a readable directory"
    return path


def _get(target, key, path, token=None, timeout=PING_TIMEOUT_SECONDS):
    bearer = target.token if token is None else token
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    return requests.get(
        target.url.rstrip("/") + "/" + key + path,
        headers=headers,
        timeout=timeout,
    )


def test_concurrent_ping_finishes_inside_healthcheck_deadline(target, terminals):
    def probe(key):
        start = time.monotonic()
        response = _get(target, key, "/ping")
        elapsed = time.monotonic() - start
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert elapsed < PING_TIMEOUT_SECONDS
        return elapsed

    keys = [entry["key"] for entry in terminals] * PROBES_PER_TERMINAL
    with ThreadPoolExecutor(max_workers=MAX_PROBE_WORKERS) as pool:
        durations = list(pool.map(probe, keys))
    print(f"concurrent ping: probes={len(keys)} max_seconds={max(durations):.3f}")


@pytest.mark.parametrize("token", ["", "EXAMPLE-DO-NOT-USE"])
def test_ping_still_requires_the_bearer_token(target, token):
    if not target.token:
        pytest.skip("target intentionally disables authentication")
    response = _get(target, target.terminal_key, "/ping", token=token)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "code": "UNAUTHORIZED"}


def test_ping_is_unlogged_but_other_requests_still_reach_full_log(target, log_dir):
    path = log_dir / "full.log"
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        offset = stream.tell()
        ping_tag = "livetest-ping-" + uuid.uuid4().hex
        busy_tag = "livetest-busy-" + uuid.uuid4().hex
        assert _get(target, target.terminal_key, "/ping?probe=" + ping_tag).status_code == 200
        assert _get(
            target, target.terminal_key, "/busy?probe=" + busy_tag,
            timeout=LOG_WAIT_SECONDS,
        ).status_code == 200
        deadline = time.monotonic() + LOG_WAIT_SECONDS
        while True:
            stream.seek(offset)
            text = stream.read().decode("utf-8", errors="replace")
            assert ping_tag not in text, "ping still writes request log lines"
            lines = [line for line in text.splitlines() if busy_tag in line]
            if any(" -> GET /busy" in line for line in lines) and any(
                " <- GET /busy" in line for line in lines
            ):
                break
            assert time.monotonic() < deadline, "non-ping request logging did not reach full.log"
            time.sleep(LOG_POLL_SECONDS)


def latest_blas_report(path: Path) -> dict:
    report = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if START_MARKER in line:
                report = None  # a prior process must not certify the current worker
            if BLAS_MARKER in line:
                payload = line.split(BLAS_MARKER, 1)[1]
                report = json.loads(payload) if payload.startswith("{") else None
    assert report is not None, "latest API process has no successful BLAS runtime record"
    return report


def test_every_configured_worker_has_one_native_blas_thread(terminals, log_dir):
    for entry in terminals:
        parts = entry["key"].split("/")
        assert len(parts) == 3 and all(
            part and part not in (".", "..") and "\\" not in part for part in parts
        ), "invalid terminal key in unifier response"
        report = latest_blas_report(log_dir / ("api-" + "-".join(parts) + ".log"))
        assert report["pid"] > 0
        assert report["env"] == {name: "1" for name in THREAD_ENV}
        assert report["libraries"], "runtime report has no native BLAS library"
        assert all(library["num_threads"] == 1 for library in report["libraries"])
    print(f"native BLAS: verified {len(terminals)} deployed workers")
