"""The terminal's pid, found by its exact executable path (chartctl/terminal_pid.py).

psutil and the WMI fallback are faked: these tests pin which process counts
as this terminal's, not Windows itself.
"""
from __future__ import annotations

import pytest

from mt5api.chartctl import terminal_pid as tp

OURS = r"C:\Users\Docker\Desktop\Shared\terminals\testbroker\acct1\default\terminal64.exe"


class _Proc:
    def __init__(self, pid, name, exe, created=100.0):
        self.pid = pid
        self.info = {"name": name, "exe": exe, "create_time": created}


def _processes(monkeypatch, procs):
    monkeypatch.setattr(tp.psutil, "process_iter", lambda attrs=None: iter(procs))


def _no_wmi(monkeypatch):
    def refuse(*_a, **_kw):
        raise AssertionError("WMI must not run when psutil can read every path")

    monkeypatch.setattr(tp.subprocess, "run", refuse)


def test_the_one_process_from_this_path_is_found(monkeypatch):
    _processes(monkeypatch, [_Proc(1, "notepad.exe", r"C:\Windows\notepad.exe"),
                             _Proc(42, "terminal64.exe", OURS, created=123.5)])
    _no_wmi(monkeypatch)

    assert tp.find_terminal(OURS) == (42, 123.5)


def test_the_host_share_spelling_of_the_same_path_matches(monkeypatch):
    on_share = r"\\host.lan\Data\terminals\testbroker\acct1\default\terminal64.exe"
    _processes(monkeypatch, [_Proc(42, "terminal64.exe", on_share)])
    _no_wmi(monkeypatch)

    assert tp.find_terminal(OURS)[0] == 42


@pytest.mark.parametrize(
    "other",
    [
        # another instance of the same account: the old no-account wildcard hit it
        r"C:\Users\Docker\Desktop\Shared\terminals\testbroker\acct1\second\terminal64.exe",
        # a sibling directory whose name starts with ours: the old substring match hit it
        r"C:\Users\Docker\Desktop\Shared\terminals\testbroker\acct1\default2\terminal64.exe",
        # another account of the broker
        r"C:\Users\Docker\Desktop\Shared\terminals\testbroker\acct2\default\terminal64.exe",
    ],
)
def test_a_terminal_from_any_other_path_is_not_ours(monkeypatch, other):
    _processes(monkeypatch, [_Proc(7, "terminal64.exe", other)])
    _no_wmi(monkeypatch)

    with pytest.raises(tp.TerminalPidError) as err:
        tp.find_terminal(OURS)
    assert err.value.code == "pid_not_found"


def test_two_processes_from_this_path_are_ambiguous(monkeypatch):
    _processes(monkeypatch, [_Proc(1, "terminal64.exe", OURS), _Proc(2, "terminal64.exe", OURS)])
    _no_wmi(monkeypatch)

    with pytest.raises(tp.TerminalPidError) as err:
        tp.find_terminal(OURS)
    assert err.value.code == "pid_ambiguous"
    assert err.value.diag == {"pids": [1, 2]}


def test_the_case_of_the_path_does_not_matter(monkeypatch):
    _processes(monkeypatch, [_Proc(42, "TERMINAL64.EXE", OURS.upper())])
    _no_wmi(monkeypatch)

    assert tp.find_terminal(OURS)[0] == 42


def test_wmi_runs_only_when_psutil_cannot_read_a_path(monkeypatch):
    _processes(monkeypatch, [_Proc(42, "terminal64.exe", None)])  # AccessDenied: exe is None
    calls = []

    class _Result:
        stdout = f"42|{OURS}\n17|C:\\elsewhere\\terminal64.exe\n"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(tp.subprocess, "run", fake_run)
    monkeypatch.setattr(tp, "_create_time", lambda pid: 55.0)

    assert tp.find_terminal(OURS) == (42, 55.0)
    assert len(calls) == 1 and calls[0][0] == "powershell"


def test_a_failed_wmi_fallback_finds_nothing(monkeypatch):
    _processes(monkeypatch, [_Proc(42, "terminal64.exe", None)])

    def broken(*_a, **_kw):
        raise OSError("no powershell")

    monkeypatch.setattr(tp.subprocess, "run", broken)

    assert tp.matching_processes(OURS) == []
