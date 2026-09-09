"""Behavioural coverage for the compose-managed vm-watchdog sidecar.

These tests exercise the decision policy against a fake Docker transport, so
they run in the offline suite (Dockerfile.test) with no docker daemon. They
cover the behaviours psyb0t asked for on PR #15: healthy/starting exclusion,
sustained unhealthy restart, image/label scoping, cooldown/backoff, bounded
retries, and reset after stable health.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vm-watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("vm_watchdog_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wd():
    module = _load()
    module.STATE_DIR = "unused-in-policy"
    return module


class _Recorder:
    """Captures recreate_vm() calls in place of the old restart log."""

    def __init__(self):
        self.calls = []
        self.raises = None

    @property
    def services(self):
        return [service for service, _project in self.calls]

    def __call__(self, service, project):
        if self.raises:
            raise self.raises
        self.calls.append((service, project))


@pytest.fixture
def recorder(wd, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(wd, "recreate_vm", rec)
    return rec


# ── Fake Docker transport ────────────────────────────────────────────────────


class _FakeClient:
    """Scripted docker client: containers list and per-id health.

    Recovery is deliberately NOT on this client any more: a restart through the
    Docker API strands the wickworks sidecar (fresh netns on start), so the
    watchdog shells out to scripts/recreate-vm.sh instead. The recreate calls
    it makes are captured by the `recorder` fixture below.
    """

    def __init__(self, containers, health=None, project="mt5-httpapi"):
        self.containers = containers
        self.health = health or {}
        self.project = project

    def list_containers(self):
        return self.containers

    def inspect(self, cid):
        if cid == "self-watchdog-id":
            return {
                "Config": {
                    "Labels": {"com.docker.compose.project": self.project}
                }
            }
        if cid in self.health:
            return {"State": {"Health": self.health[cid]}}
        return {"State": {}}



def _container(cid, name, image="dockurr/windows:5.14", labels=None):
    return {
        "Id": cid,
        "Names": [f"/{name}"],
        "Image": image,
        # The service label is what a recreate names; default it to the id so
        # the assertions below can keep talking about one identifier.
        "Labels": labels or {
            "com.docker.compose.project": "mt5-httpapi",
            "com.docker.compose.service": cid,
        },
    }


def _health(status, streak):
    return {"Status": status, "FailingStreak": streak}


# ── Healthy / starting exclusion ─────────────────────────────────────────────

def test_healthy_vm_is_never_restarted(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    cid = "aaa"
    client = _FakeClient(
        [_container(cid, "mt5")],
        health={cid: _health("healthy", 0)},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi") == 0
    assert recorder.services == []
    # healthy clock starts, but nothing is reset yet (below reset window).
    state = json.loads((tmp_path / f"{wd.state_key('mt5-httpapi', cid)}.json").read_text())
    assert state["healthy_since"] > 0
    assert state["attempts"] == 0


def test_starting_vm_is_never_restarted(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    cid = "aaa"
    client = _FakeClient(
        [_container(cid, "mt5")],
        health={cid: _health("starting", 0)},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi") == 0
    assert recorder.services == []
    # starting must not start the healthy clock either.
    state = json.loads((tmp_path / f"{wd.state_key('mt5-httpapi', cid)}.json").read_text())
    assert state["healthy_since"] == 0


# ── Sustained unhealthy threshold ────────────────────────────────────────────

def test_unhealthy_below_streak_threshold_waits(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 10
    cid = "aaa"
    client = _FakeClient(
        [_container(cid, "mt5")],
        health={cid: _health("unhealthy", 4)},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi") == 0
    assert recorder.services == []


def test_unhealthy_at_streak_threshold_restarts(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 10
    cid = "aaa"
    client = _FakeClient(
        [_container(cid, "mt5")],
        health={cid: _health("unhealthy", 10)},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi") == 1
    assert recorder.services == [cid]


# ── Image / label scoping ────────────────────────────────────────────────────

def test_only_this_projects_vm_image_containers_are_restarted(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    vm = _container("vm1", "mt5")
    other_project = _container("other1", "some-other-vm", labels={"com.docker.compose.project": "other"})
    sidecar = _container("side1", "wickworks", image="psyb0t/wickworks:v0.3.1")
    logrotator = _container("rot1", "log-rotator", image="alpine:3.20")
    client = _FakeClient(
        [vm, other_project, sidecar, logrotator],
        health={
            "vm1": _health("unhealthy", 99),
            "other1": _health("unhealthy", 99),
            "side1": _health("unhealthy", 99),
            "rot1": _health("unhealthy", 99),
        },
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi") == 1
    assert recorder.services == ["vm1"]


# ── Backoff ──────────────────────────────────────────────────────────────────

def test_exponential_backoff_between_restarts(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.BACKOFF_ATTEMPTS = [300, 900, 3600]
    wd.MAX_ATTEMPTS = 99
    cid = "aaa"
    client = _FakeClient([_container(cid, "mt5")], health={cid: _health("unhealthy", 99)})

    now = 1_000_000
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        # First restart is immediate at threshold.
        assert wd.sweep_once(client, "mt5-httpapi", now=now) == 1
        # After 1st restart, backoff tier 0 = 300s: too soon at +299, ok at +301.
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 299) == 0
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 301) == 1
        # After 2nd restart (at +301), backoff tier 1 = 900s.
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 302) == 0
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 301 + 901) == 1
        # After 3rd restart (at +1202), backoff tier 2 = 3600s.
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 1203) == 0
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 1202 + 3601) == 1

    assert recorder.services == [cid, cid, cid, cid]
    state = json.loads((tmp_path / f"{wd.state_key('mt5-httpapi', cid)}.json").read_text())
    assert state["attempts"] == 4


# ── Bounded retries ──────────────────────────────────────────────────────────

def test_gives_up_after_max_attempts(wd, tmp_path, capsys, recorder):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.MAX_ATTEMPTS = 3
    wd.BACKOFF_ATTEMPTS = [0, 0, 0]  # no backoff: three rapid attempts
    cid = "aaa"
    client = _FakeClient([_container(cid, "mt5")], health={cid: _health("unhealthy", 99)})

    now = 1_000_000
    assert wd.sweep_once(client, "mt5-httpapi", now=now) == 1
    assert wd.sweep_once(client, "mt5-httpapi", now=now + 1) == 1
    assert wd.sweep_once(client, "mt5-httpapi", now=now + 2) == 1
    # Attempt budget exhausted: no more restarts, and it logs loudly.
    assert wd.sweep_once(client, "mt5-httpapi", now=now + 3) == 0
    assert wd.sweep_once(client, "mt5-httpapi", now=now + 999_999) == 0

    assert recorder.services == [cid, cid, cid]
    assert "GIVING UP" in capsys.readouterr().out


# ── Reset after stable health ────────────────────────────────────────────────

def test_attempts_reset_after_sustained_healthy_period(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.MAX_ATTEMPTS = 1
    wd.BACKOFF_ATTEMPTS = [0]
    wd.RESET_SECONDS = 1800
    cid = "aaa"
    state_file = tmp_path / f"{wd.state_key('mt5-httpapi', cid)}.json"

    def make_client(status, streak):
        return _FakeClient([_container(cid, "mt5")], health={cid: _health(status, streak)})

    now = 1_000_000
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        # Two unhealthy sweeps: first restarts, second exceeds MAX_ATTEMPTS.
        assert wd.sweep_once(make_client("unhealthy", 99), "mt5-httpapi", now=now) == 1
        assert wd.sweep_once(make_client("unhealthy", 99), "mt5-httpapi", now=now + 1) == 0
        # The budget really is spent - one attempt recorded, one recreate made.
        # (This line used to read `assert ... or True`, which asserted nothing.)
        assert json.loads(state_file.read_text())["attempts"] == 1
        assert recorder.services == [cid]

        # VM recovers and stays healthy long enough to reset the budget.
        assert wd.sweep_once(make_client("healthy", 0), "mt5-httpapi", now=now + 100) == 0
        assert wd.sweep_once(make_client("healthy", 0), "mt5-httpapi", now=now + 100 + 1800) == 0
        state = json.loads(state_file.read_text())
        assert state["attempts"] == 0
        assert state["last_restart"] == 0

        # A later crash gets a fresh budget again.
        assert wd.sweep_once(make_client("unhealthy", 99), "mt5-httpapi", now=now + 100 + 1801) == 1
    assert recorder.services == [cid, cid]


# ── healthy_since means CONTINUOUSLY healthy ─────────────────────────────────
#
# psyb0t (2026-09-04): healthy_since survived a `starting` state, so a VM that
# was healthy before a restart had its attempt budget refunded on the first
# healthy poll AFTER it - not after RESET_SECONDS of proven stability. The same
# leak existed for an unhealthy poll below the streak threshold.


def _vm(cid, status, streak=0):
    return _FakeClient([_container(cid, "mt5")], health={cid: _health(status, streak)})


def _budget_spent_then_healthy_at(wd, cid, now):
    """Spend the single-attempt budget, then observe the VM healthy at `now`.
    Returns nothing; the caller continues the timeline."""
    assert wd.sweep_once(_vm(cid, "unhealthy", 99), "mt5-httpapi", now=now - 10) == 1
    assert wd.sweep_once(_vm(cid, "healthy"), "mt5-httpapi", now=now) == 0


@pytest.mark.parametrize(
    "interruption",
    [("starting", 0), ("unhealthy", 1), ("none", 0)],
    ids=["starting", "unhealthy-below-streak", "no-healthcheck"],
)
def test_any_non_healthy_observation_restarts_the_reset_clock(wd, tmp_path, recorder, interruption):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 10
    wd.MAX_ATTEMPTS = 1
    wd.BACKOFF_ATTEMPTS = [0]
    wd.RESET_SECONDS = 1800
    cid = "aaa"
    status, streak = interruption
    key = wd.state_key("mt5-httpapi", cid)

    t0 = 1_000_000
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        # Streak 99 so the spend is not blocked by the threshold above.
        assert wd.sweep_once(_vm(cid, "unhealthy", 99), "mt5-httpapi", now=t0 - 10) == 1
        assert wd.sweep_once(_vm(cid, "healthy"), "mt5-httpapi", now=t0) == 0
        # Interrupted 1000s in, back to healthy 1900s in: 1900s since the
        # ORIGINAL healthy poll (> RESET_SECONDS), but only 900s continuous.
        assert wd.sweep_once(_vm(cid, status, streak), "mt5-httpapi", now=t0 + 1000) == 0
        assert wd.sweep_once(_vm(cid, "healthy"), "mt5-httpapi", now=t0 + 1900) == 0
        assert wd.load_state(key)["attempts"] == 1, "budget refunded across a non-healthy state"

        # Continuity restored at t0+1900: the reset happens 1800s after THAT.
        assert wd.sweep_once(_vm(cid, "healthy"), "mt5-httpapi", now=t0 + 1900 + 1799) == 0
        assert wd.load_state(key)["attempts"] == 1
        assert wd.sweep_once(_vm(cid, "healthy"), "mt5-httpapi", now=t0 + 1900 + 1800) == 0
        assert wd.load_state(key)["attempts"] == 0

    assert recorder.services == [cid]


def test_the_healthy_clock_starts_at_the_first_healthy_poll_after_recovery(wd, tmp_path, recorder):
    """The recovery itself zeroes the clock; the next healthy poll starts it."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.BACKOFF_ATTEMPTS = [0]
    cid = "aaa"
    key = wd.state_key("mt5-httpapi", cid)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(_vm(cid, "unhealthy", 99), "mt5-httpapi", now=500) == 1
        assert wd.load_state(key)["healthy_since"] == 0
        assert wd.sweep_once(_vm(cid, "starting"), "mt5-httpapi", now=600) == 0
        assert wd.load_state(key)["healthy_since"] == 0
        assert wd.sweep_once(_vm(cid, "healthy"), "mt5-httpapi", now=700) == 0
        assert wd.load_state(key)["healthy_since"] == 700


# ── Dry-run ──────────────────────────────────────────────────────────────────

def test_dry_run_reports_without_restarting(wd, tmp_path, recorder):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    cid = "aaa"
    client = _FakeClient([_container(cid, "mt5")], health={cid: _health("unhealthy", 99)})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi", dry_run=True) == 1
    assert recorder.services == []


def test_dry_run_writes_no_state(wd, tmp_path, recorder):
    """A dry pass must not consume the real backoff or attempt budget.

    decide() mutates the state it is handed, and sweep_once used to persist it
    before branching on dry_run. So --dry-run spent attempts and stamped
    last_restart without restarting anything, and enough dry passes left the VM
    at GIVING UP the moment dry-run was switched off - the supervisor refusing
    to act precisely when it was finally allowed to.
    """
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    cid = "aaa"
    client = _FakeClient([_container(cid, "mt5")], health={cid: _health("unhealthy", 10)})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi", dry_run=True, now=1000000) == 1

    assert recorder.services == []
    written = list(tmp_path.iterdir())
    assert written == [], f"dry-run persisted state: {[p.name for p in written]}"


def test_repeated_dry_runs_do_not_exhaust_the_attempt_budget(wd, tmp_path, recorder):
    """The consequence, end to end: dry passes then a real one.

    Whatever the attempt cap is, running dry that many times and then switching
    dry-run off must still restart. Asserting on the restart rather than on a
    number keeps this honest if the cap ever moves.
    """
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    cid = "aaa"

    def fresh():
        return _FakeClient([_container(cid, "mt5")], health={cid: _health("unhealthy", 10)})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        for i in range(wd.MAX_ATTEMPTS + 2):
            wd.sweep_once(fresh(), "mt5-httpapi", dry_run=True, now=1000000 + i * 100000)
        wd.sweep_once(fresh(), "mt5-httpapi", dry_run=False, now=2000000)

    assert recorder.services == [cid], "dry runs consumed the budget for the real one"


def test_a_real_run_still_persists_state(wd, tmp_path, recorder):
    """The other direction: skipping persistence must be dry-run only."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    cid = "aaa"
    client = _FakeClient([_container(cid, "mt5")], health={cid: _health("unhealthy", 10)})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        wd.sweep_once(client, "mt5-httpapi", dry_run=False, now=1000000)

    assert recorder.services == [cid]
    state = wd.load_state(wd.state_key("mt5-httpapi", cid))
    assert state["attempts"] == 1
    assert state["last_restart"] == 1000000


# ── Project self-discovery ───────────────────────────────────────────────────

def test_compose_project_discovered_from_own_labels(wd):
    client = _FakeClient([], project="mt5-httpapi")
    wd.SELF_ID = "self-watchdog-id"
    assert wd._compose_project(client) == "mt5-httpapi"


def test_recreate_failure_keeps_state_for_backoff(wd, tmp_path, recorder):
    """A failed recreate must still record the attempt + timestamp so the
    backoff logic prevents an immediate retry loop."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.BACKOFF_ATTEMPTS = [300]
    cid = "aaa"
    client = _FakeClient(
        [_container(cid, "mt5")],
        health={cid: _health("unhealthy", 99)},
    )
    recorder.raises = RuntimeError("boom")
    now = 1_000_000
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi", now=now) == 1
        # Recreate failed, but the state now enforces the backoff window.
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 10) == 0
        assert wd.sweep_once(client, "mt5-httpapi", now=now + 301) == 1
    assert recorder.services == []


# ── Configuration parsing ────────────────────────────────────────────────────
#
# The daemon holds the Docker socket, so bad configuration has to stop it at
# startup rather than surface as a crash mid-sweep. Reported by psyb0t: an
# empty WATCHDOG_BACKOFF_ATTEMPTS survived startup as [] and then raised
# IndexError inside decide() on the first unhealthy pass after a restart.

_REPO = SCRIPT.resolve().parents[1]


def _load_with(monkeypatch, **env):
    for key in list(os.environ):
        if key.startswith("WATCHDOG_"):
            monkeypatch.delenv(key, raising=False)
    # The recovery wiring is supplied by the compose service, not defaulted in
    # the script, so provide it here the way the shipped compose does. Tests
    # that care about it missing pass it explicitly.
    env.setdefault("WATCHDOG_PROJECT_DIR", str(_REPO))
    env.setdefault("WATCHDOG_RECREATE_SCRIPT", str(_REPO / "scripts" / "recreate-vm.sh"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return _load()


def test_empty_backoff_list_is_refused_at_startup(monkeypatch):
    """The reported crash. [] survived startup, then IndexError'd in decide()."""
    wd = _load_with(monkeypatch, WATCHDOG_BACKOFF_ATTEMPTS="")
    problems = wd.validate_config()
    assert any("WATCHDOG_BACKOFF_ATTEMPTS" in p for p in problems), problems
    assert wd.main() == 2


def test_a_refused_config_never_reaches_the_indexing_that_crashed(monkeypatch):
    """Defence in depth: even if validation were bypassed, the fallback value
    must be usable rather than empty, so decide() cannot raise IndexError."""
    wd = _load_with(monkeypatch, WATCHDOG_BACKOFF_ATTEMPTS="")
    assert wd.BACKOFF_ATTEMPTS, "fell back to an empty list"
    state = {"attempts": 1, "last_restart": 1000, "healthy_since": 0}
    wd.decide(state, "unhealthy", 99, 1000)  # must not raise


@pytest.mark.parametrize("value", ["", "   ", "abc", "0", "-1"])
def test_bad_interval_values_are_reported(monkeypatch, value):
    wd = _load_with(monkeypatch, WATCHDOG_INTERVAL_SECONDS=value)
    assert any("WATCHDOG_INTERVAL_SECONDS" in p for p in wd.validate_config()), value


@pytest.mark.parametrize(
    "name,value",
    [
        ("WATCHDOG_MIN_FAILING_STREAK", "0"),
        ("WATCHDOG_MAX_ATTEMPTS", "0"),
        ("WATCHDOG_RESET_SECONDS", "0"),
        ("WATCHDOG_BACKOFF_ATTEMPTS", "300,0"),
        ("WATCHDOG_BACKOFF_ATTEMPTS", "300,-5"),
        ("WATCHDOG_BACKOFF_ATTEMPTS", "300,abc"),
    ],
)
def test_zero_and_negative_are_rejected_where_they_make_no_sense(monkeypatch, name, value):
    wd = _load_with(monkeypatch, **{name: value})
    assert any(name in p for p in wd.validate_config()), (name, value)


def test_an_empty_image_filter_is_refused(monkeypatch):
    """Not in the report, same class of bug and worse consequences.

    "".startswith() matches every image, so a blank filter would make every
    container in the project a restart candidate - the watchdog itself, and any
    database sharing the project.
    """
    wd = _load_with(monkeypatch, WATCHDOG_IMAGE_FILTER="")
    assert any("WATCHDOG_IMAGE_FILTER" in p for p in wd.validate_config())
    assert wd.main() == 2


def test_every_problem_is_reported_at_once(monkeypatch):
    """One message listing all of them beats fixing them one traceback at a time."""
    wd = _load_with(
        monkeypatch,
        WATCHDOG_BACKOFF_ATTEMPTS="",
        WATCHDOG_INTERVAL_SECONDS="0",
        WATCHDOG_MAX_ATTEMPTS="nope",
    )
    problems = wd.validate_config()
    assert len(problems) >= 3, problems


def test_a_valid_configuration_reports_nothing(monkeypatch):
    wd = _load_with(
        monkeypatch,
        WATCHDOG_BACKOFF_ATTEMPTS="60, 120 ,240",
        WATCHDOG_INTERVAL_SECONDS="15",
        WATCHDOG_MAX_ATTEMPTS="5",
    )
    assert wd.validate_config() == []
    assert wd.BACKOFF_ATTEMPTS == [60, 120, 240]
    assert wd.INTERVAL_SECONDS == 15


def test_defaults_alone_are_valid(monkeypatch):
    """The shipped compose sets almost nothing, so the defaults must pass."""
    wd = _load_with(monkeypatch)
    assert wd.validate_config() == []


# ── Coordinated recreate ─────────────────────────────────────────────────────
#
# Why recovery is a recreate and not a restart: a sidecar sharing the VM's netns
# (`network_mode: service:<vm>`) resolves that binding once, at its own start.
# Restarting the owner keeps its container id but gives it a FRESH netns, so the
# sidecar is left on a dead one — tests/integration/test_wickworks_lifecycle.py
# asserts exactly that. Only recreating the owner together with its sidecars
# repairs it, which is what scripts/recreate-vm.sh does.


def test_recovery_names_the_compose_service_not_the_container_id(wd, tmp_path, recorder):
    """recreate-vm.sh takes compose service names; a container id means nothing
    to compose, and passing one would recreate nothing at all."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    cid = "9f3c1a2b4d5e"
    client = _FakeClient(
        [_container(cid, "mt5", labels={
            "com.docker.compose.project": "mt5-httpapi",
            "com.docker.compose.service": "mt5-fast",
        })],
        health={cid: _health("unhealthy", 99)},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi", now=1_000_000) == 1
    assert recorder.calls == [("mt5-fast", "mt5-httpapi")]


def test_a_vm_without_a_service_label_is_skipped_not_guessed(wd, tmp_path, recorder):
    """Compose always sets the label; something hand-run may not. Skipping is
    the honest outcome — inventing a service name would recreate the wrong
    thing, or silently nothing."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    cid = "aaa"
    client = _FakeClient(
        [_container(cid, "mt5", labels={"com.docker.compose.project": "mt5-httpapi"})],
        health={cid: _health("unhealthy", 99)},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi", now=1_000_000) == 0
    assert recorder.calls == []


# ── State survives the recreate it triggered ─────────────────────────────────

def _same_service_as(cid, service="mt5"):
    """One VM container for the compose service, under whatever id Docker
    assigned this incarnation."""
    return _container(cid, "mt5", labels={
        "com.docker.compose.project": "mt5-httpapi",
        "com.docker.compose.service": service,
    })


def test_the_attempt_cap_survives_the_recreate_it_triggered(wd, tmp_path, recorder, capsys):
    """Recovery REPLACES the container, so the next poll sees a new id.

    State used to be keyed by container id: the recreate orphaned the record
    that had just spent an attempt, the replacement loaded a fresh one, and a
    persistently-broken VM was recovered forever at "attempt 1" - the cap and
    backoff reset themselves on every recovery they were meant to bound.
    """
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.MAX_ATTEMPTS = 1
    wd.BACKOFF_ATTEMPTS = [999_999]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        old = _FakeClient([_same_service_as("old-container-id")],
                          health={"old-container-id": _health("unhealthy", 10)})
        assert wd.sweep_once(old, "mt5-httpapi", now=1_000_000) == 1

        # The recreate happened: same compose service, replacement id, still
        # unhealthy. The single-attempt budget is already spent.
        new = _FakeClient([_same_service_as("new-container-id")],
                          health={"new-container-id": _health("unhealthy", 10)})
        assert wd.sweep_once(new, "mt5-httpapi", now=1_000_010) == 0

    assert recorder.services == ["mt5"], "the replacement id refunded the attempt budget"


def test_backoff_survives_the_recreate_it_triggered(wd, tmp_path, recorder):
    """Same replacement-id scenario, asserted on backoff rather than the cap:
    inside the backoff window the replacement must wait, and once the window
    passes it is recovered again - carrying the attempt count forward."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.MAX_ATTEMPTS = 99
    wd.BACKOFF_ATTEMPTS = [300]

    def incarnation(cid):
        return _FakeClient([_same_service_as(cid)], health={cid: _health("unhealthy", 10)})

    now = 1_000_000
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(incarnation("id-1"), "mt5-httpapi", now=now) == 1
        # Replacement, inside the 300s window: waits.
        assert wd.sweep_once(incarnation("id-2"), "mt5-httpapi", now=now + 200) == 0
        # Window passed: second attempt, on yet another id.
        assert wd.sweep_once(incarnation("id-3"), "mt5-httpapi", now=now + 301) == 1

    state = wd.load_state(wd.state_key("mt5-httpapi", "mt5"))
    assert state["attempts"] == 2, "attempts must accumulate across container ids"


def test_state_key_is_stable_identity_not_container_id(wd):
    assert wd.state_key("proj", "mt5") == wd.state_key("proj", "mt5")
    assert wd.state_key("proj", "mt5") != wd.state_key("proj", "mt5-b")
    # Labels are the only outside text that becomes a file name. With every
    # separator replaced the key is a single path component, so it cannot
    # traverse out of the state directory no matter what a label carries.
    hostile = wd.state_key("pro/ject", "../../etc/passwd")
    assert "/" not in hostile and "\\" not in hostile


def test_recreate_refuses_without_the_project_path(wd, monkeypatch):
    """Compose resolves this project's relative bind mounts client-side, so
    without the host path the recreate would rewrite every mount. Refuse loudly
    rather than run a compose that quietly mounts the wrong things."""
    monkeypatch.setattr(wd, "PROJECT_DIR", "")
    with pytest.raises(RuntimeError, match="WATCHDOG_PROJECT_DIR"):
        wd.recreate_vm("mt5", "mt5-httpapi")


def test_recreate_hands_the_helper_the_project_name_and_the_project_dir(wd, monkeypatch, tmp_path):
    """Two variables compose needs that the container was never handed.

    COMPOSE_PROJECT_NAME: compose derives the project from the directory name
    otherwise, and a mismatch does not fail — it creates a SECOND set of
    containers beside the running ones.

    MT5_PROJECT_DIR: the compose file interpolates `${MT5_PROJECT_DIR:?}` on
    every compose command. This test used to assert only the project name, so
    the helper failing at interpolation - psyb0t's 2026-09-04 blocker - was
    invisible to it. The host variable is scrubbed first: the helper must get
    it from the watchdog, not by inheritance from whoever ran the tests.
    """
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = dict(kwargs.get("env") or {})

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.delenv("MT5_PROJECT_DIR", raising=False)
    monkeypatch.setattr(wd, "PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(wd, "RECREATE_SCRIPT", "/project/scripts/recreate-vm.sh")
    monkeypatch.setattr(wd.subprocess, "run", fake_run)

    wd.recreate_vm("mt5", "mt5-httpapi")

    assert seen["cmd"] == ["/project/scripts/recreate-vm.sh", "mt5"]
    assert seen["cwd"] == str(tmp_path)
    assert seen["env"]["COMPOSE_PROJECT_NAME"] == "mt5-httpapi"
    assert seen["env"]["MT5_PROJECT_DIR"] == str(tmp_path)


def test_recreate_env_is_the_watchdogs_own_project_dir_not_the_hosts(wd, monkeypatch, tmp_path):
    """Even when the host shell HAS the variable, the helper gets the
    watchdog's WATCHDOG_PROJECT_DIR: that is the path the project is mounted at
    inside this container, which is what compose must resolve mounts against."""
    monkeypatch.setenv("MT5_PROJECT_DIR", "/somewhere/else/entirely")
    monkeypatch.setattr(wd, "PROJECT_DIR", str(tmp_path))
    env = wd.recreate_env("mt5-httpapi")
    assert env["MT5_PROJECT_DIR"] == str(tmp_path)
    assert env["COMPOSE_PROJECT_NAME"] == "mt5-httpapi"
    # Everything else is inherited: the helper needs PATH, HOME, DOCKER_HOST...
    assert env["PATH"] == os.environ["PATH"]


def test_recreate_surfaces_the_scripts_failure(wd, monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no such service: mt5"

        return R()

    monkeypatch.setattr(wd, "PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="no such service"):
        wd.recreate_vm("mt5", "mt5-httpapi")


def test_missing_project_dir_is_reported_at_startup(monkeypatch):
    """Finding out recovery cannot work only once a VM has crashed is the worst
    possible time to learn it."""
    wd = _load_with(monkeypatch, WATCHDOG_PROJECT_DIR="")
    assert any("WATCHDOG_PROJECT_DIR" in p for p in wd.validate_config())


def test_missing_recreate_script_is_reported_at_startup(monkeypatch):
    wd = _load_with(monkeypatch, WATCHDOG_RECREATE_SCRIPT="/nope/recreate-vm.sh")
    assert any("WATCHDOG_RECREATE_SCRIPT" in p for p in wd.validate_config())


# ── The REAL helper, under the watchdog's EXACT child environment ────────────
#
# psyb0t (2026-09-04): the unit suite passed while every real recovery failed,
# because nothing here ever ran scripts/recreate-vm.sh with the environment the
# watchdog actually gives it. These do. The docker CLI is a stub - the offline
# image has no daemon - but the stub emulates the one compose behaviour that
# matters: `${VAR:?msg}` interpolation of the compose file fails BEFORE any
# container is touched when VAR is unset or empty. Everything else in the chain
# is real: the watchdog's env construction, the bash script, its PyYAML sidecar
# discovery, the compose file.

_COMPOSE_REQUIRING_PROJECT_DIR = """\
services:
  mt5:
    image: dockurr/windows:5.14
  wickworks:
    image: psyb0t/wickworks
    network_mode: "service:mt5"
  vm-watchdog:
    image: python:3.12-alpine
    volumes:
      - ${MT5_PROJECT_DIR:?MT5_PROJECT_DIR must be the absolute host path of this project}:${MT5_PROJECT_DIR}:ro
"""

_STUB_DOCKER = r"""#!/bin/sh
# Stand-in for the docker CLI. Emulates compose's `${VAR:?msg}` interpolation:
# when the compose file names MT5_PROJECT_DIR as required and it is unset or
# empty in THIS process's environment, fail exactly as compose does, before
# doing anything. Otherwise record the call and succeed.
printf 'argv: %s\n' "$*" >>"$STUB_LOG"
env | grep -E '^(MT5_PROJECT_DIR|COMPOSE_PROJECT_NAME)=' >>"$STUB_LOG" || true
file=""
prev=""
for a in "$@"; do
    [ "$prev" = "-f" ] && file="$a"
    prev="$a"
done
if [ -n "$file" ] && grep -q 'MT5_PROJECT_DIR:?' "$file" && [ -z "${MT5_PROJECT_DIR:-}" ]; then
    echo 'error while interpolating services.vm-watchdog.volumes.[]: required variable MT5_PROJECT_DIR is missing a value' >&2
    exit 1
fi
exit 0
"""


def _project_with_stub_docker(tmp_path):
    """A compose project holding the REAL helper, plus a stub docker on PATH."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    helper = project / "scripts" / "recreate-vm.sh"
    shutil.copy(_REPO / "scripts" / "recreate-vm.sh", helper)
    helper.chmod(0o755)
    (project / "docker-compose.yml").write_text(_COMPOSE_REQUIRING_PROJECT_DIR, encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(_STUB_DOCKER, encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    return project, helper, bindir, tmp_path / "stub.log"


def test_the_real_helper_recreates_under_the_watchdogs_exact_child_environment(wd, monkeypatch, tmp_path):
    """The blocker, end to end: recreate_vm() -> real recreate-vm.sh -> docker
    compose, with MT5_PROJECT_DIR scrubbed from the host so the ONLY way the
    helper can have it is the watchdog putting it there."""
    project, helper, bindir, log = _project_with_stub_docker(tmp_path)
    monkeypatch.delenv("MT5_PROJECT_DIR", raising=False)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setattr(wd, "PROJECT_DIR", str(project))
    monkeypatch.setattr(wd, "RECREATE_SCRIPT", str(helper))

    wd.recreate_vm("mt5", "mt5-httpapi")  # raises RuntimeError on any failure

    recorded = log.read_text(encoding="utf-8")
    assert f"MT5_PROJECT_DIR={project}" in recorded
    assert "COMPOSE_PROJECT_NAME=mt5-httpapi" in recorded
    # The real script planned the real operation, with the sidecar it found
    # in the compose file - not some path the stub short-circuited.
    assert "compose" in recorded and "stop" in recorded
    assert "--force-recreate" in recorded
    assert "wickworks" in recorded


def test_without_the_injected_variable_the_same_helper_fails_at_interpolation(tmp_path, monkeypatch):
    """Guards the test above. Run the identical helper WITHOUT MT5_PROJECT_DIR
    and it fails with compose's own error, before stopping anything - so a
    regression to the old child environment turns the suite red rather than
    quietly green."""
    project, helper, bindir, log = _project_with_stub_docker(tmp_path)
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "COMPOSE_PROJECT_NAME": "mt5-httpapi",
        # No MT5_PROJECT_DIR - the pre-fix child environment.
    }
    res = subprocess.run(
        [str(helper), "mt5"], cwd=str(project), env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert res.returncode != 0
    assert "required variable MT5_PROJECT_DIR is missing a value" in res.stderr
    assert "--force-recreate" not in log.read_text(encoding="utf-8"), "recreate ran despite the failed stop"


# ── Scope: exact image repository, and never itself ──────────────────────────
#
# psyb0t (2026-08-21), still open at c29b289: `startswith(IMAGE_FILTER)` also
# matched `dockurr/windows-not-the-vm`, and nothing excluded SELF_ID, so a valid
# operator override like WATCHDOG_IMAGE_FILTER=python selected the watchdog.


@pytest.mark.parametrize(
    "image,selected",
    [
        ("dockurr/windows", True),
        ("dockurr/windows:5.14", True),
        ("dockurr/windows@sha256:" + "ab" * 32, True),
        ("dockurr/windows-not-the-vm:latest", False),
        ("dockurr/windows2:1", False),
        ("psyb0t/wickworks:v0.3.1", False),
        ("notdockurr/windows:5.14", False),
    ],
)
def test_image_filter_matches_the_repository_exactly(wd, tmp_path, recorder, image, selected):
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.IMAGE_FILTER = "dockurr/windows"
    client = _FakeClient([_container("c1", "vm", image=image)], health={"c1": _health("unhealthy", 99)})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        wd.sweep_once(client, "mt5-httpapi", now=1_000_000)
    assert recorder.services == (["c1"] if selected else []), image


def test_the_watchdog_never_selects_itself_whatever_the_filter_says(wd, tmp_path, recorder):
    """Docker sets the hostname to the SHORT container id; the list endpoint
    reports the full one. Self-exclusion is unconditional and first, before
    the image filter gets a say."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.IMAGE_FILTER = "python"
    wd.SELF_ID = "abcdef123456"
    me = _container("abcdef123456" + "0" * 52, "vm-watchdog", image="python:3.12-alpine")
    vm = _container("vm1", "fake-vm", image="python:3.12-alpine")
    client = _FakeClient(
        [me, vm],
        health={me["Id"]: _health("unhealthy", 99), "vm1": _health("unhealthy", 99)},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi", now=1_000_000) == 1
    assert recorder.services == ["vm1"]


def test_an_empty_self_id_excludes_nothing_by_accident(wd):
    """"".startswith("") is True for every id: an unset SELF_ID must not make
    every container look like the watchdog and silently disable it."""
    wd.SELF_ID = ""
    wd.SELF_FULL_ID = ""
    assert wd._is_self("anything") is False


def test_self_exclusion_uses_the_resolved_full_id_when_the_hostname_is_a_name(wd, tmp_path, recorder):
    """`hostname: watchdog` on the service (or WATCHDOG_SELF_ID set to a name)
    used to defeat self-exclusion entirely, because the prefix test compared
    container ids against a word. With the full id resolved at startup the
    test is equality, and the hostname no longer matters."""
    wd.STATE_DIR = str(tmp_path)
    wd.MIN_FAILING_STREAK = 1
    wd.IMAGE_FILTER = "python"
    wd.SELF_ID = "watchdog"
    full = "f" * 64
    wd.SELF_FULL_ID = full
    me = _container(full, "vm-watchdog", image="python:3.12-alpine")
    vm = _container("vm1", "fake-vm", image="python:3.12-alpine")
    client = _FakeClient([me, vm], health={full: _health("unhealthy", 99), "vm1": _health("unhealthy", 99)})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wd, "log", lambda *a, **k: None)
        assert wd.sweep_once(client, "mt5-httpapi", now=1_000_000) == 1
    assert recorder.services == ["vm1"]


def test_a_name_hostname_without_a_resolved_id_never_prefix_matches_other_containers(wd):
    """The fallback is only the hex short-id prefix. A word must not exclude a
    container whose id merely starts with the same letters."""
    wd.SELF_FULL_ID = ""
    wd.SELF_ID = "abc"           # too short / not a container id
    assert wd._is_self("abc" + "0" * 61) is False
    wd.SELF_ID = "watchdog"
    assert wd._is_self("watchdog-lookalike") is False
    wd.SELF_ID = "abcdef123456"  # a real short id still works by prefix
    assert wd._is_self("abcdef123456" + "0" * 52) is True


def test_main_resolves_its_own_full_id_before_the_first_sweep(monkeypatch):
    """The resolution is what makes the equality test possible in production."""
    wd = _load_with(monkeypatch, WATCHDOG_COMPOSE_PROJECT="mt5-httpapi")
    wd.SELF_ID = "self-watchdog-id"

    class Client(_FakeClient):
        def inspect(self, cid):
            if cid == "self-watchdog-id":
                return {"Id": "e" * 64, "Config": {"Labels": {"com.docker.compose.project": "mt5-httpapi"}}}
            return super().inspect(cid)

    monkeypatch.setattr(wd, "DockerClient", lambda *_a, **_k: Client([]))
    # Stop main() at its first sleep, after startup work is done.
    monkeypatch.setattr(wd.time, "sleep", lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(wd, "log", lambda *a, **k: None)
    with pytest.raises(KeyboardInterrupt):
        wd.main()
    assert wd.SELF_FULL_ID == "e" * 64
