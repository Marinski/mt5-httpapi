"""Tester process cleanup: timeout kill and boot sweep.

MT5 runs a test as terminal64.exe plus one metatester64.exe per agent, and the
agents are what bind the localhost ports. subprocess.run kills the process it
started but neither the agents nor a terminal MT5 relaunched in place of ours,
so a timed-out run used to leave those ports held and every later run on that
terminal died with "bind error [10048]" until the host was rebooted.
"""
from __future__ import annotations

import psutil
import pytest

from mt5api.backtest import handler


class FakeProc:
    def __init__(self, name, exe, dies_on_terminate=True):
        self.info = {"name": name, "exe": exe}
        self.dies_on_terminate = dies_on_terminate
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


@pytest.fixture
def terminal_dir(monkeypatch, tmp_path):
    directory = tmp_path / "terminals" / "darwinex" / "live" / "a"
    directory.mkdir(parents=True)
    monkeypatch.setattr(handler, "TERMINAL_DIR", str(directory))
    return str(directory)


def _install(monkeypatch, procs, still_alive=()):
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: list(procs))
    monkeypatch.setattr(
        psutil, "wait_procs", lambda victims, timeout=None: ([], list(still_alive))
    )


def test_kills_the_terminal_and_its_agents(monkeypatch, terminal_dir):
    terminal = FakeProc("terminal64.exe", f"{terminal_dir}\\terminal64.exe")
    agent_one = FakeProc("metatester64.exe", f"{terminal_dir}\\metatester64.exe")
    agent_two = FakeProc("metatester64.exe", f"{terminal_dir}\\metatester64.exe")
    _install(monkeypatch, [terminal, agent_one, agent_two])

    assert handler.kill_terminal_processes() == 3
    assert all(p.terminated for p in (terminal, agent_one, agent_two))


def test_never_touches_a_sibling_terminals_processes(monkeypatch, terminal_dir):
    sibling = terminal_dir[:-1] + "b"
    mine = FakeProc("terminal64.exe", f"{terminal_dir}\\terminal64.exe")
    theirs = FakeProc("terminal64.exe", f"{sibling}\\terminal64.exe")
    theirs_agent = FakeProc("metatester64.exe", f"{sibling}\\metatester64.exe")
    _install(monkeypatch, [mine, theirs, theirs_agent])

    assert handler.kill_terminal_processes() == 1
    assert mine.terminated
    assert not theirs.terminated and not theirs_agent.terminated


def test_never_touches_a_sibling_whose_dir_shares_our_prefix(monkeypatch, terminal_dir):
    # `a` is a component boundary: a sibling at `a2` or `aa` sorts/prefixes
    # after us but is a different terminal directory. A substring match would
    # terminate theirs too.
    for suffix in ("2", "a"):
        sibling = terminal_dir + suffix
        theirs = FakeProc("terminal64.exe", f"{sibling}\\terminal64.exe")
        theirs_agent = FakeProc("metatester64.exe", f"{sibling}\\metatester64.exe")
        mine = FakeProc("terminal64.exe", f"{terminal_dir}\\terminal64.exe")
        _install(monkeypatch, [mine, theirs, theirs_agent])

        assert handler.kill_terminal_processes() == 1
        assert mine.terminated
        assert not theirs.terminated and not theirs_agent.terminated


def test_ignores_unrelated_processes(monkeypatch, terminal_dir):
    noise = FakeProc("chrome.exe", f"{terminal_dir}\\chrome.exe")
    _install(monkeypatch, [noise])
    assert handler.kill_terminal_processes() == 0
    assert not noise.terminated


def test_escalates_to_kill_when_terminate_is_ignored(monkeypatch, terminal_dir):
    stubborn = FakeProc("terminal64.exe", f"{terminal_dir}\\terminal64.exe")
    _install(monkeypatch, [stubborn], still_alive=[stubborn])

    assert handler.kill_terminal_processes() == 1
    assert stubborn.terminated and stubborn.killed


def test_reports_nothing_to_do_when_the_terminal_is_idle(monkeypatch, terminal_dir):
    _install(monkeypatch, [])
    assert handler.kill_terminal_processes() == 0


def test_alive_check_ignores_agents(monkeypatch, terminal_dir):
    # Agents outliving their terminal must not read as "a run is still going",
    # or _await_self_relaunch would wait out the full job timeout on them.
    agent = FakeProc("metatester64.exe", f"{terminal_dir}\\metatester64.exe")
    _install(monkeypatch, [agent])
    assert handler._terminal_process_alive() is False

    terminal = FakeProc("terminal64.exe", f"{terminal_dir}\\terminal64.exe")
    _install(monkeypatch, [terminal])
    assert handler._terminal_process_alive() is True


def test_survives_a_process_vanishing_mid_scan(monkeypatch, terminal_dir):
    class Vanishing(FakeProc):
        def terminate(self):
            raise psutil.NoSuchProcess(pid=1)

    gone = Vanishing("terminal64.exe", f"{terminal_dir}\\terminal64.exe")
    _install(monkeypatch, [gone])
    assert handler.kill_terminal_processes() == 1


# ── Startup cleanup ──────────────────────────────────────────────────────────


@pytest.fixture
def startup(monkeypatch):
    """_run_backtest_startup_cleanup with its collaborators stubbed."""
    # mt5api.main pulls in the WSGI/MCP stack; skip where those are not
    # installed rather than fail (the container test image installs them).
    pytest.importorskip("a2wsgi")
    from mt5api import main

    calls = {"killed": 0}

    def fake_kill(*_args, **_kwargs):
        calls["killed"] += 1
        return 1

    monkeypatch.setattr(handler, "kill_terminal_processes", fake_kill)
    monkeypatch.setattr(main.backtest_jobs, "prune_old_jobs", lambda *a, **k: 0)
    return main, calls


def test_kills_leftovers_even_when_nothing_was_swept(startup, monkeypatch):
    """The case a sweep-gated kill misses.

    A run whose state file is older than the sweep lookback — an API killed
    while a long test was live — is never swept, so gating the kill on the
    sweep leaves that process holding this terminal's agent ports forever.
    """
    main, calls = startup
    monkeypatch.setattr(main, "MODE", "backtest")
    monkeypatch.setattr(main.backtest_jobs, "sweep_orphans", lambda *a, **k: 0)

    main._run_backtest_startup_cleanup()

    assert calls["killed"] == 1


def test_kills_leftovers_when_jobs_were_swept(startup, monkeypatch):
    main, calls = startup
    monkeypatch.setattr(main, "MODE", "backtest")
    monkeypatch.setattr(main.backtest_jobs, "sweep_orphans", lambda *a, **k: 3)

    main._run_backtest_startup_cleanup()

    assert calls["killed"] == 1


def test_never_kills_in_live_mode(startup, monkeypatch):
    """The live terminal is meant to stay up, and this thread runs in every mode."""
    main, calls = startup
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setattr(main.backtest_jobs, "sweep_orphans", lambda *a, **k: 2)

    main._run_backtest_startup_cleanup()

    assert calls["killed"] == 0
