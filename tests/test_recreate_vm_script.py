"""Tests for scripts/recreate-vm.sh.

The script had no direct coverage, and the gap hid a production failure: it
recreated VMs with `docker compose up --force-recreate`, whose implicit stop
uses compose's own --timeout (10s by default) rather than the service's
stop_grace_period. A dockurr/windows VM cannot shut down in 10s, so compose
tried to remove a still-running container and the recreate failed:

    cannot remove container "...": container is running

These exercise --dry-run, so they assert the planned command line without
needing a Docker daemon.
"""

import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "recreate-vm.sh"

COMPOSE = """\
services:
  mt5:
    image: dockurr/windows:5.14
    stop_grace_period: 2m
  wickworks:
    image: psyb0t/wickworks
    network_mode: "service:mt5"
  mt5-b:
    image: dockurr/windows:5.14
  wickworks-b:
    image: psyb0t/wickworks
    network_mode: "service:mt5-b"
  unrelated:
    image: nginx
"""


def run(tmp_path, *args, env=None):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(COMPOSE, encoding="utf-8")
    # Inherit the real PATH: the script's sidecar discovery shells out to
    # python3, which is not at a fixed location across host and test image.
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "COMPOSE_FILE": str(compose)}
    full_env.update(env or {})
    return subprocess.run(
        [str(_SCRIPT), "--dry-run", *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
        check=False,
    )


def test_stops_before_recreating(tmp_path):
    """The stop must be explicit, or compose removes a running container."""
    res = run(tmp_path, "mt5")
    assert res.returncode == 0, res.stderr
    assert "docker compose stop" in res.stdout
    stop_at = res.stdout.index("docker compose stop")
    up_at = res.stdout.index("docker compose up")
    assert stop_at < up_at, "stop must be planned before the recreate"


def test_stop_timeout_matches_the_grace_period_by_default(tmp_path):
    """10s (compose's default) is far too short for a Windows VM."""
    res = run(tmp_path, "mt5")
    assert "-t 120" in res.stdout
    assert "-t 10 " not in res.stdout


def test_timeout_is_passed_to_up_as_well(tmp_path):
    """`up --force-recreate` does its own stop; it must not use the 10s default."""
    res = run(tmp_path, "mt5")
    up_line = next(ln for ln in res.stdout.splitlines() if "docker compose up" in ln)
    assert "--force-recreate" in up_line
    assert "--no-deps" in up_line
    assert "-t 120" in up_line


def test_timeout_is_overridable(tmp_path):
    res = run(tmp_path, "mt5", env={"RECREATE_STOP_TIMEOUT": "45"})
    assert "-t 45" in res.stdout
    assert "-t 120" not in res.stdout


def test_sidecars_are_recreated_with_their_vm(tmp_path):
    """The whole point: the sidecar must rejoin the VM's new netns."""
    res = run(tmp_path, "mt5")
    up_line = next(ln for ln in res.stdout.splitlines() if "docker compose up" in ln)
    assert "mt5" in up_line
    assert "wickworks" in up_line
    assert "unrelated" not in up_line
    assert "mt5-b" not in up_line


def test_multiple_vms_expand_to_all_their_sidecars(tmp_path):
    res = run(tmp_path, "mt5", "mt5-b")
    up_line = next(ln for ln in res.stdout.splitlines() if "docker compose up" in ln)
    for expected in ("mt5", "wickworks", "mt5-b", "wickworks-b"):
        assert expected in up_line
    assert "unrelated" not in up_line


def test_no_arguments_is_an_error(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(COMPOSE, encoding="utf-8")
    res = subprocess.run(
        [str(_SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "COMPOSE_FILE": str(compose)},
        timeout=60,
        check=False,
    )
    assert res.returncode != 0
