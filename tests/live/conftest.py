"""Live suite fixtures: the target stack comes from the environment.

Run with `make test-live`. Required:

    MT5_LIVE_URL      server root, e.g. http://127.0.0.1:8888
    MT5_LIVE_BROKER   broker of the terminal to test
    MT5_LIVE_ACCOUNT  account of the terminal to test

Optional:

    MT5_LIVE_TOKEN        API bearer token (empty when auth is off)
    MT5_LIVE_INSTANCE     terminal instance (default "default")
    MT5_LIVE_SYMBOL       symbol for market data and deployments (default EURUSD)
    MT5_LIVE_TIMEFRAME    timeframe for the test deployment (default M15)
    MT5_LIVE_ALLOW_REAL   "1" to run state-changing tests on a non-demo account
    MT5_LIVE_WEBREQUEST   "1" to test changing the WebRequest allowlist
    MT5_LIVE_TRADING      "1" to also run tests/real (places real orders)

Values may also live in tests/live/.env (gitignored); real environment
variables win. Every artifact the suite creates is named with ARTIFACT_PREFIX
and removed before and after the run.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

from tests.live.live_client import (
    LiveAPIError,
    McpClient,
    RestClient,
    Target,
    terminal_mcp,
    unified_mcp,
)

ARTIFACT_PREFIX = "livetest-"
ACCOUNT_TRADE_MODE_DEMO = 0
_ENABLED = "1"
_HTTP_NOT_FOUND = 404
_REQUIRED_ENV = ("MT5_LIVE_URL", "MT5_LIVE_BROKER", "MT5_LIVE_ACCOUNT")


def _load_env_file() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()


def flag(name: str) -> bool:
    return os.environ.get(name, "") == _ENABLED


@pytest.fixture(scope="session")
def target() -> Target:
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.exit(f"live suite needs {', '.join(missing)} (see tests/live/conftest.py)", 2)
    return Target(
        url=os.environ["MT5_LIVE_URL"],
        token=os.environ.get("MT5_LIVE_TOKEN", ""),
        broker=os.environ["MT5_LIVE_BROKER"],
        account=os.environ["MT5_LIVE_ACCOUNT"],
        instance=os.environ.get("MT5_LIVE_INSTANCE") or "default",
    )


@pytest.fixture(scope="session")
def settings() -> dict[str, str]:
    return {
        "symbol": os.environ.get("MT5_LIVE_SYMBOL") or "EURUSD",
        "timeframe": os.environ.get("MT5_LIVE_TIMEFRAME") or "M15",
    }


@pytest.fixture(scope="session")
def rest(target: Target) -> RestClient:
    client = RestClient(target)
    client.get("/ping")
    return client


@pytest.fixture(scope="session")
def mcp_terminal(target: Target) -> McpClient:
    return terminal_mcp(target)


@pytest.fixture(scope="session")
def mcp_unified(target: Target) -> McpClient:
    return unified_mcp(target)


@pytest.fixture(scope="session")
def account(rest: RestClient) -> dict:
    return rest.get("/account")


@pytest.fixture(scope="session")
def chartctl_enabled(rest: RestClient) -> bool:
    """Whether the target serves Chart Deployments, judged by its routes."""
    try:
        rest.request("GET", "/loader")
    except LiveAPIError as err:
        if f"-> {_HTTP_NOT_FOUND}" in str(err):
            return False
        raise
    return True


@pytest.fixture(scope="session")
def state_changes_allowed(account: dict) -> None:
    """Skip a state-changing test on a non-demo account unless allowed."""
    if account.get("trade_mode") == ACCOUNT_TRADE_MODE_DEMO or flag("MT5_LIVE_ALLOW_REAL"):
        return
    pytest.skip("target account is not a demo account; set MT5_LIVE_ALLOW_REAL=1 to run")


@pytest.fixture(scope="session")
def chartctl(chartctl_enabled: bool, state_changes_allowed: None, mcp_unified: McpClient):
    """The unified MCP client for chartctl tests, with livetest- artifacts
    purged before and after the session."""
    if not chartctl_enabled:
        pytest.skip("Chart Deployments are not enabled on the target terminal")
    purge_artifacts(mcp_unified)
    yield mcp_unified
    purge_artifacts(mcp_unified)


def purge_artifacts(mcp: McpClient) -> None:
    """Delete every deployment and expert the suite created."""
    for dep in mcp.call_json("list_deployments")["deployments"]:
        if dep["desired"].get("expert_file", "").startswith(ARTIFACT_PREFIX):
            mcp.call_json("delete_deployment", deployment_id=dep["id"])
    for expert in mcp.call_json("list_experts")["experts"]:
        if expert["source"] != "uploaded" or not expert["name"].startswith(ARTIFACT_PREFIX):
            continue
        try:
            mcp.call_json("delete_expert", name=expert["name"])
        except LiveAPIError as err:
            # Still referenced while its deployment's chart closes; the purge
            # at the other end of the session gets it.
            warnings.warn(f"left {expert['name']} staged for the next purge: {err}", stacklevel=2)
