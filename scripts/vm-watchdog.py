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

- A tiny JSON state record per VM lives on a named volume, keyed by STABLE
  COMPOSE IDENTITY (``/state/<project>.<service>.json``): last restart time,
  attempt count, and when the VM was last observed healthy. Never keyed by
  container id: recovery here is a recreate, which REPLACES the container, so
  an id-keyed record is orphaned by the very action that wrote it and the next
  poll would start the replacement at ``attempts = 0`` - every recovery
  silently refunding the attempt budget and resetting backoff.
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
and the watchdog itself are never touched.

Recovery is a COORDINATED RECREATE, not a restart. A sidecar sharing the VM's
network namespace (``network_mode: service:<vm>``, i.e. wickworks) resolves
that binding once, at its own start, into an immutable
``NetworkMode=container:<owner-id>``. Restarting the owner keeps its id, but
Docker tears the netns down on stop and builds a fresh one on start, so the
sidecar is left holding a dead namespace. This module previously claimed the
opposite; ``tests/integration/test_wickworks_lifecycle.py`` disproves it,
asserting that BOTH "recreate owner alone" and "restart owner alone" strand
the sidecar, and that only recreating the owner together with its sidecars
repairs the binding.

So the watchdog does not restart through the Docker API. It shells out to
``scripts/recreate-vm.sh`` — the same helper an operator runs by hand, and the
one that lifecycle test covers — which discovers each VM's sidecars from the
generated Compose file and recreates them together.

That is why this container needs the compose project mounted at the SAME
absolute path the host uses: Compose resolves relative bind mounts
client-side, so ``./scripts/x`` in the compose file has to land on the host's
``<project>/scripts/x``, not on some path inside this container. ``run.sh``
exports ``MT5_PROJECT_DIR`` for that. Without it the watchdog refuses to act
and says so, rather than falling back to a restart that looks like recovery
and is not.

Runs forever, restarting itself if it crashes: keepalive is the Compose
``restart: unless-stopped`` on this service, not a supervisor inside the loop.
"""

from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# ── Configuration (env-overridable, defaults match the sidecar in compose) ──

# Misconfiguration is collected rather than raised, so importing this module
# never explodes and validate_config() can report EVERY problem at once. An
# operator fixing one variable at a time from successive tracebacks is a worse
# afternoon than one message listing all of them.
CONFIG_ERRORS: list[str] = []


def _env_int(name: str, default: str, minimum: int) -> int:
    """Integer from the environment, or the default plus a recorded error.

    Falling back to the default keeps the module importable; validate_config()
    is what refuses to run. Blank is rejected explicitly: an unset variable and
    one set to "" mean different things to the person who wrote the compose
    file, and only the second is a mistake worth naming.
    """
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    text = raw.strip()
    if not text:
        CONFIG_ERRORS.append(f"{name} is set but empty; expected an integer >= {minimum}")
        return int(default)
    try:
        value = int(text)
    except ValueError:
        CONFIG_ERRORS.append(f"{name}={raw!r} is not an integer")
        return int(default)
    if value < minimum:
        CONFIG_ERRORS.append(f"{name}={value} is below the minimum of {minimum}")
        return int(default)
    return value


def _env_int_list(name: str, default: str, minimum: int) -> list[int]:
    """Comma-separated positive integers, never empty.

    The empty case is the one that mattered: WATCHDOG_BACKOFF_ATTEMPTS= parsed
    to [], which survived startup and then raised IndexError inside decide()
    on the first unhealthy pass after a recorded restart - a crash loop in the
    thing that exists to recover from crashes.
    """
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        CONFIG_ERRORS.append(
            f"{name}={raw!r} parsed to an empty list; expected at least one integer >= {minimum}"
        )
        return [int(p) for p in default.split(",")]
    values: list[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            CONFIG_ERRORS.append(f"{name} entry {part!r} is not an integer")
            continue
        if value < minimum:
            CONFIG_ERRORS.append(f"{name} entry {value} is below the minimum of {minimum}")
            continue
        values.append(value)
    if not values:
        return [int(p) for p in default.split(",")]
    return values


def _env_str(name: str, default: str, *, allow_empty: bool) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.strip() and not allow_empty:
        CONFIG_ERRORS.append(f"{name} is set but empty; expected a value")
        return default
    return raw


DOCKER_SOCKET = _env_str("WATCHDOG_DOCKER_SOCKET", "/var/run/docker.sock", allow_empty=False)

# The coordinated recreate helper, and the compose project it must act on.
# PROJECT_DIR is the HOST path: compose resolves the relative bind mounts in
# docker-compose.yml against it, so a container-local path would rewrite every
# mount to somewhere that does not exist on the host. Empty means "not wired
# up" and is reported by validate_config() rather than guessed at.
RECREATE_SCRIPT = _env_str(
    "WATCHDOG_RECREATE_SCRIPT", "/project/scripts/recreate-vm.sh", allow_empty=False
)
PROJECT_DIR = _env_str("WATCHDOG_PROJECT_DIR", "", allow_empty=True)
RECREATE_TIMEOUT_SECONDS = _env_int("WATCHDOG_RECREATE_TIMEOUT", "300", minimum=30)
STATE_DIR = _env_str("WATCHDOG_STATE_DIR", "/state", allow_empty=False)
INTERVAL_SECONDS = _env_int("WATCHDOG_INTERVAL_SECONDS", "30", minimum=1)

# Restart only after this many consecutive failed healthchecks.
MIN_FAILING_STREAK = _env_int("WATCHDOG_MIN_FAILING_STREAK", "10", minimum=1)

# Only containers whose image starts with this are considered VM containers.
# Empty is refused rather than treated as "no filter": "".startswith() matches
# every image, so a blank value would make every container in the project a
# restart candidate - including this watchdog and any database sharing it.
IMAGE_FILTER = _env_str("WATCHDOG_IMAGE_FILTER", "dockurr/windows", allow_empty=False)

# Exponential backoff per attempt: first restart after 5m, then 15m, then 1h.
BACKOFF_ATTEMPTS = _env_int_list("WATCHDOG_BACKOFF_ATTEMPTS", "300,900,3600", minimum=1)

# Give up (and log loudly) after this many consecutive failed recoveries.
MAX_ATTEMPTS = _env_int("WATCHDOG_MAX_ATTEMPTS", "3", minimum=1)

# A VM must stay healthy this long before its attempt budget resets. Zero is
# refused: the budget would reset on the first healthy poll after a restart,
# which silently makes MAX_ATTEMPTS unreachable and the give-up path dead code.
RESET_SECONDS = _env_int("WATCHDOG_RESET_SECONDS", "1800", minimum=1)

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
    """A tiny read-only Docker API client.

    Only the endpoints the watchdog needs are implemented: list containers and
    inspect one. Recovery deliberately does NOT go through this client — see
    recreate_vm() and the module docstring. Anything else the daemon replies
    with is surfaced as a RuntimeError carrying the status + body.
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



def recreate_vm(service: str, project: str) -> None:
    """Recreate one VM together with every sidecar sharing its netns.

    Delegates to scripts/recreate-vm.sh instead of reimplementing it: that
    script is what operators run, what the lifecycle regression test exercises,
    and the only place that knows how to find a VM's sidecars in the generated
    compose file. Reimplementing the discovery here would give the fleet two
    recovery paths that could drift apart.

    COMPOSE_PROJECT_NAME is passed explicitly. Compose otherwise derives the
    project from the directory name, and a mismatch there would not fail — it
    would quietly create a SECOND set of containers alongside the running ones.
    """
    if not PROJECT_DIR:
        raise RuntimeError(
            "WATCHDOG_PROJECT_DIR is unset, so the compose project cannot be "
            "recreated. Start the stack through run.sh (it exports "
            "MT5_PROJECT_DIR), or set it to the host path of the project."
        )
    env = dict(os.environ, COMPOSE_PROJECT_NAME=project)
    result = subprocess.run(
        [RECREATE_SCRIPT, service],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=RECREATE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(
            f"{RECREATE_SCRIPT} {service} exited {result.returncode}: {detail}"
        )


# ── Pure policy: state + health in, action out ───────────────────────────────


def state_key(project: str, service: str) -> str:
    """Stable identity for one VM's state record.

    Compose project + service survives a recreate; the container id does not.
    Sanitized defensively so a hostile-looking label cannot become a path -
    both values come from compose labels, but this file name is the only place
    they touch the filesystem.
    """
    raw = f"{project}.{service}"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in raw)


def load_state(key: str, state_dir: str | None = None) -> dict:
    """The per-VM state record, or a fresh one when absent/corrupt."""
    path = Path(state_dir if state_dir is not None else STATE_DIR) / f"{key}.json"
    if not path.exists():
        return {"last_restart": 0, "attempts": 0, "healthy_since": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_restart": 0, "attempts": 0, "healthy_since": 0}


def save_state(key: str, state: dict, state_dir: str | None = None) -> None:
    path = Path(state_dir if state_dir is not None else STATE_DIR) / f"{key}.json"
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
    """One pass over the project's VM containers. Returns recovery count."""
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
        # Resolve the compose service BEFORE touching state: it is both what a
        # recreate names and the stable half of the state key. A recreate
        # replaces the container, so state keyed by container id was orphaned
        # by every successful recovery - the replacement arrived with a fresh
        # id, loaded a fresh record, and the attempt cap and backoff never
        # carried across the one boundary they exist to police.
        service = (c.get("Labels") or {}).get("com.docker.compose.service")
        if not service:
            log(f"{name}: no compose service label; cannot recreate, skipping")
            continue
        state = load_state(state_key(project, service))
        # decide() MUTATES the state it is handed - it increments attempts and
        # stamps last_restart. Under --dry-run that must not reach disk: a dry
        # pass would consume the real backoff and attempt budget without
        # restarting anything, so enough dry passes leave the VM at GIVING UP
        # the moment dry-run is switched off. Evaluate against a copy instead,
        # and persist nothing.
        working = copy.deepcopy(state) if dry_run else state
        action, reason = decide(working, status, streak, now)
        if not dry_run:
            save_state(state_key(project, service), working)
        if action == "restart":
            # Recreate by COMPOSE SERVICE, not container id: the helper has to
            # name the VM and its sidecars as compose services to recreate them
            # together, and a container id means nothing to compose.
            if dry_run:
                log(f"DRY-RUN: would recreate {service} (+ its sidecars) - {reason}")
            else:
                try:
                    recreate_vm(service, project)
                    log(f"recreated {service} and its sidecars - {reason}")
                except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    log(f"{name}: recreate failed ({exc}); state kept for backoff")
            restarted += 1
        elif action == "give_up":
            log(f"GIVING UP on {name} after {working.get('attempts', 0)} attempts - {reason}")
    return restarted


def validate_config() -> list[str]:
    """Every configuration problem found at import, as human-readable lines.

    Separate from main() so a test can assert on the messages without running
    the daemon, and so an operator can see all of them in one go.

    The recovery wiring is checked here rather than at the moment a VM dies:
    finding out that the watchdog cannot act only once something has already
    crashed is the worst possible time to learn it.
    """
    errors = list(CONFIG_ERRORS)
    if not PROJECT_DIR:
        errors.append(
            "WATCHDOG_PROJECT_DIR is empty - recovery needs the compose project's "
            "HOST path (run.sh exports MT5_PROJECT_DIR). Without it a crashed VM "
            "cannot be recreated."
        )
    elif not os.path.isdir(PROJECT_DIR):
        errors.append(
            f"WATCHDOG_PROJECT_DIR={PROJECT_DIR!r} is not a directory in this "
            "container - mount the project through at the same absolute path the "
            "host uses, or compose will rewrite every relative bind mount."
        )
    if not os.path.exists(RECREATE_SCRIPT):
        errors.append(
            f"WATCHDOG_RECREATE_SCRIPT={RECREATE_SCRIPT!r} not found - the "
            "coordinated recreate helper must be mounted into this container."
        )
    return errors


def main() -> int:
    problems = validate_config()
    if problems:
        # Refuse to start rather than run on defaults. This daemon restarts
        # containers; quietly substituting values an operator did not choose is
        # the wrong failure mode for something holding the Docker socket.
        log(f"invalid configuration ({len(problems)} problem(s)); refusing to start")
        for problem in problems:
            log(f"  {problem}")
        return 2

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
