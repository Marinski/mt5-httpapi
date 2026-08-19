#!/usr/bin/env python3
"""Compose-managed VM crash watchdog.

dockurr/windows keeps the container "up" while the Windows guest may have
crashed internally (e.g. Event ID 6008 "The previous system shutdown ... was
unexpected"). Docker's healthcheck then reports ``unhealthy``, but because the
container never exits, ``restart: unless-stopped`` never fires and every
terminal API inside that VM stays dead until a human restarts it. This sidecar
is that human, running as a normal Compose service instead of a host cron job.

It polls Docker health through the mounted unix socket and restarts a VM
container ONLY after its Docker health has stayed ``unhealthy`` for a sustained
``FailingStreak`` — the same source of truth ``docker inspect`` exposes. A
healthy or merely-starting VM is never touched, so long-running backtests on a
working VM are never interrupted; the healthcheck goes green the whole time a
terminal is serving, and only a genuinely dead VM stays red.

Restart policy is stateful, not a fixed cooldown:

- A tiny JSON state record per container lives on a named volume
  (``/state/<container-id>.json``): last restart time, attempt count, and when
  the VM was last observed healthy.
- After the sustained-unhealthy threshold, the VM is restarted. Attempts gate
  an exponential backoff (default 5m -> 15m -> 1h): a VM that crashes again
  immediately after recovery is not restarted into a loop.
- After ``WATCHDOG_MAX_ATTEMPTS`` consecutive failed recoveries the watchdog
  stops trying and logs loudly, so a genuinely broken VM does not thresh forever.
- Attempts reset only after the VM has stayed healthy for
  ``WATCHDOG_RESET_SECONDS``, so a VM that recovered then crashed again later
  gets a fresh budget instead of inheriting old failures.

Scope is strict: only containers in this Compose project running the
``dockurr/windows`` image are considered, so nginx, wickworks, the log rotator
and the watchdog itself are never touched. ``docker restart`` (not recreate) is
used, keeping the owner container ID and therefore the wickworks sidecar's
netns attachment intact.

Runs forever, restarting itself if it crashes: keepalive is the Compose
``restart: unless-stopped`` on this service, not a supervisor inside the loop.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import time
from pathlib import Path

# ── Configuration (env-overridable, defaults match the sidecar in compose) ──

DOCKER_SOCKET = os.environ.get("WATCHDOG_DOCKER_SOCKET", "/var/run/docker.sock")
STATE_DIR = os.environ.get("WATCHDOG_STATE_DIR", "/state")
INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_INTERVAL_SECONDS", "30"))

# Restart only after this many consecutive failed healthchecks.
MIN_FAILING_STREAK = int(os.environ.get("WATCHDOG_MIN_FAILING_STREAK", "10"))

# Only containers whose image starts with this are considered VM containers.
IMAGE_FILTER = os.environ.get("WATCHDOG_IMAGE_FILTER", "dockurr/windows")

# Exponential backoff per attempt: first restart after 5m, then 15m, then 1h.
BACKOFF_ATTEMPTS = [
    int(x)
    for x in os.environ.get(
        "WATCHDOG_BACKOFF_ATTEMPTS", "300,900,3600"
    ).split(",")
    if x.strip()
]

# Give up (and log loudly) after this many consecutive failed recoveries.
MAX_ATTEMPTS = int(os.environ.get("WATCHDOG_MAX_ATTEMPTS", "3"))

# A VM must stay healthy this long before its attempt budget resets.
RESET_SECONDS = int(os.environ.get("WATCHDOG_RESET_SECONDS", "1800"))

# Compose project to scope to. Empty = auto-discover from this container's own
# labels (the watchdog runs as a service in the same project).
COMPOSE_PROJECT = os.environ.get("WATCHDOG_COMPOSE_PROJECT", "")

# This container's own ID; Docker sets the container hostname to it.
SELF_ID = os.environ.get("WATCHDOG_SELF_ID", socket.gethostname())


def log(message: str, *args) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[{stamp}] [vm-watchdog] {message}" if not args else f"[{stamp}] [vm-watchdog] {message % args}")


# ── Minimal Docker Engine API client over the unix socket (stdlib only) ──────


class _UnixSocketConnection(http.client.HTTPConnection):
    """HTTPConnection that talks to the Docker Engine over a unix socket."""

    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._socket_path)
        self.sock = sock


class DockerClient:
    """A tiny read-restart Docker API client.

    Only the endpoints the watchdog needs are implemented: list containers,
    inspect one container, restart one container. Anything else the daemon
    replies with is surfaced as a RuntimeError carrying the status + body.
    """

    def __init__(self, socket_path: str = DOCKER_SOCKET):
        self._socket_path = socket_path

    def _request(self, method: str, path: str, body: str | None = None) -> bytes:
        conn = _UnixSocketConnection(self._socket_path)
        try:
            conn.request(method, path, body=body)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
        finally:
            conn.close()
        if not (200 <= status < 300):
            raise RuntimeError(
                f"docker {method} {path}: HTTP {status}: {data[:300]!r}"
            )
        return data

    def list_containers(self) -> list[dict]:
        """Running containers (Docker's default ``/containers/json``)."""
        data = self._request("GET", "/containers/json")
        return json.loads(data) if data else []

    def inspect(self, container_id: str) -> dict:
        data = self._request("GET", f"/containers/{container_id}/json")
        return json.loads(data)

    def restart(self, container_id: str) -> None:
        self._request("POST", f"/containers/{container_id}/restart")


# ── Pure policy: state + health in, action out ───────────────────────────────


def load_state(container_id: str, state_dir: str | None = None) -> dict:
    """The per-container state record, or a fresh one when absent/corrupt."""
    path = Path(state_dir if state_dir is not None else STATE_DIR) / f"{container_id}.json"
    if not path.exists():
        return {"last_restart": 0, "attempts": 0, "healthy_since": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_restart": 0, "attempts": 0, "healthy_since": 0}


def save_state(container_id: str, state: dict, state_dir: str | None = None) -> None:
    path = Path(state_dir if state_dir is not None else STATE_DIR) / f"{container_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def decide(state: dict, health_status: str, failing_streak: int, now: int) -> tuple[str, str]:
    """One container's next action given its state and Docker health.

    Mutates ``state`` (attempts / timestamps) and returns ``(action, reason)``
    where action is one of ``wait``, ``restart`` or ``give_up``.
    """
    if health_status != "unhealthy":
        # Healthy or starting: never touch. Track when we last saw it healthy
        # so a recovery that holds long enough resets the attempt budget.
        if health_status == "healthy":
            state["healthy_since"] = state.get("healthy_since") or now
            if now - state["healthy_since"] >= RESET_SECONDS:
                state["attempts"] = 0
                state["last_restart"] = 0
                return "wait", "healthy long enough - attempt budget reset"
        else:
            # starting / none: nothing to do, do not start a healthy clock.
            state["healthy_since"] = state.get("healthy_since") or 0
        return "wait", f"health={health_status}"

    # Unhealthy from here on.
    if failing_streak < MIN_FAILING_STREAK:
        return "wait", f"unhealthy streak {failing_streak} < {MIN_FAILING_STREAK}"

    attempts = state.get("attempts", 0)
    if attempts >= MAX_ATTEMPTS:
        return "give_up", f"attempts {attempts} >= MAX_ATTEMPTS {MAX_ATTEMPTS}"

    last_restart = state.get("last_restart", 0)
    # Delay before the NEXT restart, indexed by how many restarts already
    # happened: after the 1st restart wait BACKOFF[0], after the 2nd wait
    # BACKOFF[1], etc. First restart has no prior wait.
    delay = BACKOFF_ATTEMPTS[max(0, min(attempts - 1, len(BACKOFF_ATTEMPTS) - 1))] if attempts else 0
    if last_restart and (now - last_restart) < delay:
        return "wait", f"backoff {now - last_restart}s < {delay}s (attempt {attempts + 1})"

    state["attempts"] = attempts + 1
    state["last_restart"] = now
    state["healthy_since"] = 0
    return "restart", f"unhealthy streak {failing_streak} >= {MIN_FAILING_STREAK} (attempt {attempts + 1})"


# ── Docker-facing logic ──────────────────────────────────────────────────────


def _compose_project(client: DockerClient) -> str | None:
    """This project's name, from our own container's compose labels."""
    try:
        info = client.inspect(SELF_ID)
    except (RuntimeError, OSError):
        return None
    return (info.get("Config", {}).get("Labels", {}) or {}).get("com.docker.compose.project")


def _scoped_containers(client: DockerClient, project: str) -> list[dict]:
    """Running containers in this project running the VM image."""
    out = []
    for c in client.list_containers():
        labels = c.get("Labels", {}) or {}
        if project and labels.get("com.docker.compose.project") != project:
            continue
        if not (c.get("Image") or "").startswith(IMAGE_FILTER):
            continue
        out.append(c)
    return out


def _health_of(client: DockerClient, container_id: str) -> tuple[str, int]:
    """(State.Health.Status, State.Health.FailingStreak) or ('none', 0)."""
    info = client.inspect(container_id)
    health = info.get("State", {}).get("Health", {}) or {}
    status = health.get("Status", "none")
    streak = health.get("FailingStreak", 0)
    return status, int(streak or 0)


def sweep_once(client: DockerClient, project: str, dry_run: bool = False, now: int | None = None) -> int:
    """One pass over the project's VM containers. Returns restart count."""
    now = now if now is not None else int(time.time())
    restarted = 0
    for c in _scoped_containers(client, project):
        cid = c["Id"]
        name = (c.get("Names") or ["?"])[0]
        try:
            status, streak = _health_of(client, cid)
        except (RuntimeError, OSError) as exc:
            log(f"{name}: cannot inspect health ({exc}); skipping")
            continue
        state = load_state(cid)
        action, reason = decide(state, status, streak, now)
        save_state(cid, state)
        if action == "restart":
            if dry_run:
                log(f"DRY-RUN: would restart {name} - {reason}")
            else:
                try:
                    client.restart(cid)
                    log(f"restarted {name} - {reason}")
                except (RuntimeError, OSError) as exc:
                    log(f"{name}: restart failed ({exc}); state kept for backoff")
            restarted += 1
        elif action == "give_up":
            log(f"GIVING UP on {name} after {state.get('attempts', 0)} attempts - {reason}")
    return restarted


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log("dry-run mode: will report decisions without restarting")
    client = DockerClient(DOCKER_SOCKET)

    project = COMPOSE_PROJECT or _compose_project(client) or ""
    if not project:
        log("cannot determine compose project (no WATCHDOG_COMPOSE_PROJECT and "
            "self-inspect found no compose labels); refusing to run un-scoped")
        return 1
    log(f"scoped to compose project '{project}' (image filter '{IMAGE_FILTER}')")

    while True:
        try:
            sweep_once(client, project, dry_run=dry_run)
        except (RuntimeError, OSError) as exc:
            log(f"sweep failed: {exc}")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
