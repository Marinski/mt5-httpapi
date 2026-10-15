"""This terminal's terminal64.exe process, found by its exact executable path.

Window messages and process memory go to one process id, so the pid must be
this terminal's and no other. The old lookup matched ``ExecutablePath -like``
a wildcard and took the first hit: with no account configured the filter was
``*\\<broker>\\*``, which matches every account and instance of the broker,
and the ``broker/account/terminal64.exe`` layout never matched the
``\\<instance>\\`` filter at all. A wrong pid plus a full-replace allowlist
apply would wipe another terminal's list, so this module compares the whole
path and wants exactly one process.

The VM reaches the terminals through ``C:\\Users\\Docker\\Desktop\\Shared``,
a link to ``\\\\host.lan\\Data``, and a process may report either spelling
(``scripts/api-rolling-restart.py`` handles the same), so both are accepted.

psutil first: the API process runs elevated, so it can read the elevated
terminals' paths, and a lookup takes milliseconds. PowerShell and WMI only
when psutil cannot read a candidate's path (AccessDenied), since they take up
to 30 seconds.
"""

from __future__ import annotations

import os
import subprocess

import psutil

from mt5api.config import TERMINAL_PATH
from mt5api.logger import log

TERMINAL_EXE = "terminal64.exe"

# The VM's shared folder and the host share it links to, lower case, as
# normalised paths compare them.
_SHARED_SPELLINGS = (r"c:\users\docker\desktop\shared", r"\\host.lan\data")

# WMI can take long on a VM booting many terminals; past this the lookup fails.
_WMI_TIMEOUT_SECONDS = 30


class TerminalPidError(Exception):
    """The terminal's process could not be pinned down.

    ``code`` is ``pid_not_found`` (no process runs from TERMINAL_PATH) or
    ``pid_ambiguous`` (more than one does)."""

    def __init__(self, code: str, message: str, diag: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.diag = diag or {}


def _norm(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").lower()


def spellings(path: str) -> set[str]:
    """Every normalised spelling of ``path``: as given, its real path, and
    with the shared folder swapped for the host share and back."""
    out = set()
    # realpath only means something on Windows; elsewhere it would prefix the
    # working directory to a Windows path.
    candidates = {path, os.path.realpath(path)} if os.name == "nt" else {path}
    for p in candidates:
        n = _norm(p)
        out.add(n)
        for here in _SHARED_SPELLINGS:
            if n.startswith(here + "\\"):
                rest = n[len(here) :]
                out.update(there + rest for there in _SHARED_SPELLINGS)
    return out


def _psutil_matches(targets: set[str]) -> tuple[list[tuple[int, float]], bool]:
    """(matches as (pid, create_time), whether a candidate's path was unreadable)."""
    matches: list[tuple[int, float]] = []
    denied = False
    for proc in psutil.process_iter(["name", "exe", "create_time"]):
        info = proc.info
        if (info.get("name") or "").lower() != TERMINAL_EXE:
            continue
        exe = info.get("exe")
        if not exe:
            denied = True  # process_iter gives None for an attribute it may not read
            continue
        if spellings(exe) & targets:
            matches.append((proc.pid, float(info.get("create_time") or 0.0)))
    return matches, denied


def _create_time(pid: int) -> float:
    try:
        return float(psutil.Process(pid).create_time())
    except psutil.Error:
        return 0.0


def _wmi_matches(targets: set[str]) -> list[tuple[int, float]]:
    ps_cmd = (
        "Get-WmiObject Win32_Process -Filter \"Name='" + TERMINAL_EXE + "'\" "
        '| ForEach-Object { "$($_.ProcessId)|$($_.ExecutablePath)" }'
    )
    result = subprocess.run(
        ["powershell", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=_WMI_TIMEOUT_SECONDS,
    )
    matches = []
    for line in result.stdout.splitlines():
        pid_text, _, exe = line.strip().partition("|")
        if not pid_text.isdigit() or not exe:
            continue
        if spellings(exe) & targets:
            pid = int(pid_text)
            matches.append((pid, _create_time(pid)))
    return matches


def matching_processes(terminal_path: str = TERMINAL_PATH) -> list[tuple[int, float]]:
    """Every running process started from ``terminal_path``, as (pid, create_time)."""
    targets = spellings(terminal_path)
    matches, denied = _psutil_matches(targets)
    if matches or not denied:
        return matches
    try:
        return _wmi_matches(targets)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Terminal pid lookup: WMI fallback failed (%s)", exc)
        return []


def find_terminal(terminal_path: str = TERMINAL_PATH) -> tuple[int, float]:
    """(pid, create_time) of the one process started from ``terminal_path``.

    Raises TerminalPidError when there is none or more than one."""
    matches = matching_processes(terminal_path)
    if not matches:
        raise TerminalPidError("pid_not_found", "the terminal's process is not running")
    if len(matches) > 1:
        raise TerminalPidError(
            "pid_ambiguous",
            "more than one process runs from the terminal's path",
            {"pids": [pid for pid, _ in matches]},
        )
    return matches[0]
