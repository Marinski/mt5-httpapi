"""Integration test: the real Compose lifecycle behind the wickworks sidecar.

This is the regression psyb0t asked for on PR #13. A `network_mode:
service:<owner>` sidecar resolves its netns binding ONCE, at container start,
into an immutable `NetworkMode=container:<owner-id>`. The review reproduced
that `docker compose up -d --force-recreate <owner>` (even with deps) gives
the owner a new ID while leaving the sidecar pointed at the deleted one — the
sidecar's own healthcheck cannot repair that binding, so the "autonomous
self-heal" claim was false and harmful (the killed sidecar could never rejoin,
exactly the production failure that left wickworks-b dead for 46h).

The correct operation is to recreate the owner TOGETHER with its sidecar. This
suite stands up the real `wickworks-healthcheck.py` on a disposable compose
project and proves:

1. baseline `up -d`            -> sidecar healthy (shares the live netns)
2. recreate owner ALONE        -> sidecar orphaned: healthcheck fails and
                                  NetworkMode still names the deleted owner
3. recreate owner + sidecar    -> sidecar rejoins: healthcheck healthy, and
                                  NetworkMode == the current owner id
4. restart owner ALONE         -> also strands the sidecar's network (the
                                  owner gets a fresh netns on start), so only
                                  the recreate-together operation is reliable

Note on "restart is safe": the review claimed a plain
`docker compose restart mt5` keeps the sidecar healthy because the owner ID
does not change. That does NOT hold in this runtime — Docker tears the owner's
netns down on stop and creates a fresh one on start, so the sidecar, still
attached to the old inode, loses its eth0. The regression documents this so
operators do not rely on restart as a recovery path.

The emulation mirrors production: the OWNER holds the "gateway" ports (like the
dockurr gateway services owned by the mt5 container), and the SIDECAR serves
its own /health (like wickworks' own uvicorn). When the owner is recreated, its
listeners move to the new netns, so the orphaned sidecar still answers its own
/health but can no longer reach the gateway ports — the exact shape the real
healthcheck's orphan branch detects.

Runs on the host (`make test-integration`) because it drives the real
`docker compose` CLI through the host docker socket.
"""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .host_shared_fixture import create_host_shared_fixture_dir

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTHCHECK = REPO_ROOT / "scripts" / "wickworks-healthcheck.py"
PYTHON_IMAGE = "python:3.12-alpine"

GATEWAY_PORTS = "8011,8012,8013,8014"
PROJECT = "wickworks-lifecycle-test"

STARTUP_TIMEOUT_SECONDS = 60


def _require_compose():
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if shutil.which("docker-compose") is None:
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True)
        if probe.returncode != 0:
            pytest.skip("docker compose not available")


def _compose(project_dir, project, *args, check=True):
    cmd = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(project_dir / "docker-compose.yml"),
        *args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(
            f"{' '.join(cmd)} failed (rc={result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _owner_server():
    """Holds the gateway ports open (dockurr gateway role). Moving netns on
    recreate is what the sidecar's healthcheck detects."""
    return f"""import socket, threading

def listen(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(16)
    while True:
        c, _ = s.accept()
        c.close()

for p in [int(x) for x in "{GATEWAY_PORTS}".split(",")]:
    threading.Thread(target=listen, args=(p,), daemon=True).start()

import time
while True:
    time.sleep(1)
"""


def _sidecar_server():
    """Serves the sidecar's OWN /health, like wickworks' uvicorn. This is what
    keeps the orphan branch distinguishable from the self-down branch: the
    loopback endpoint stays green while the gateway probes fail."""
    return """from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *args):
        pass

HTTPServer(("0.0.0.0", 8000), H).serve_forever()
"""


OWNER_SCRIPT_PATH = "/owner.py"
SIDECAR_SCRIPT_PATH = "/sidecar.py"
HEALTHCHECK_SCRIPT_PATH = "/wickworks-healthcheck.py"


def _write_project(project_dir):
    (project_dir / "owner.py").write_text(_owner_server(), encoding="utf-8")
    (project_dir / "sidecar.py").write_text(_sidecar_server(), encoding="utf-8")
    (project_dir / "docker-compose.yml").write_text(
        f"""services:
  owner:
    image: {PYTHON_IMAGE}
    command: ["python3", "-u", "{OWNER_SCRIPT_PATH}"]
    volumes:
      - ./owner.py:{OWNER_SCRIPT_PATH}:ro
  sidecar:
    image: {PYTHON_IMAGE}
    command: ["python3", "-u", "{SIDECAR_SCRIPT_PATH}"]
    network_mode: "service:owner"
    depends_on:
      - owner
    volumes:
      - ./sidecar.py:{SIDECAR_SCRIPT_PATH}:ro
      - {HEALTHCHECK}:{HEALTHCHECK_SCRIPT_PATH}:ro
    environment:
      WICKWORKS_GATEWAY_HOST: "127.0.0.1"
      WICKWORKS_GATEWAY_PORTS: "{GATEWAY_PORTS}"
""",
        encoding="utf-8",
    )


def _healthcheck_exit():
    result = subprocess.run(
        ["docker", "exec", f"{PROJECT}-sidecar-1", "python3", HEALTHCHECK_SCRIPT_PATH],
        capture_output=True,
        text=True,
    )
    return result.returncode


def _wait_healthy():
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _healthcheck_exit() == 0:
            return
        time.sleep(1)
    raise TimeoutError(
        f"sidecar never became healthy within {STARTUP_TIMEOUT_SECONDS}s"
    )


def _owner_id():
    return _inspect("owner", "{{.Id}}")


def _sidecar_netmode():
    return _inspect("sidecar", "{{.HostConfig.NetworkMode}}")


def _inspect(service, tmpl):
    name = f"{PROJECT}-{service}-1"
    result = subprocess.run(
        ["docker", "inspect", "--format", tmpl, name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"docker inspect {name} failed: {result.stderr}")
    return result.stdout.strip()


@pytest.fixture(scope="module")
def lifecycle():
    _require_compose()
    project_dir = create_host_shared_fixture_dir(REPO_ROOT, "wickworks-lifecycle")
    try:
        _write_project(project_dir)
        _compose(project_dir, PROJECT, "up", "-d")
        _wait_healthy()
        yield project_dir
    finally:
        _compose(project_dir, PROJECT, "down", "-v", "--remove-orphans", check=False)
        shutil.rmtree(project_dir)


def test_sidecar_starts_healthy(lifecycle):
    """Baseline: sidecar shares the live owner netns, healthcheck exits 0."""
    assert _healthcheck_exit() == 0
    assert _sidecar_netmode() == f"container:{_owner_id()}"


def test_recreating_owner_alone_orphans_the_sidecar(lifecycle):
    """The bug psyb0t reproduced: recreate owner without the sidecar. The
    owner gets a NEW id; the sidecar keeps its stale NetworkMode (old id,
    now deleted) and its healthcheck fails — it cannot rejoin by itself.
    """
    owner_before = _owner_id()
    _compose(lifecycle, PROJECT, "up", "-d", "--force-recreate", "--no-deps", "owner")

    assert _owner_id() != owner_before, "owner should have been recreated"
    assert _sidecar_netmode() == f"container:{owner_before}", (
        "sidecar must still reference the DELETED owner id (immutable binding)"
    )
    assert _healthcheck_exit() == 1, "orphaned sidecar must report unhealthy"


def test_recreating_owner_with_sidecar_rejoins(lifecycle):
    """The correct operation: recreate owner AND sidecar together. Compose
    creates the sidecar with NetworkMode=container:<new-owner-id>, so it
    rejoins the new netns and the healthcheck goes healthy again.
    """
    _compose(
        lifecycle, PROJECT, "up", "-d", "--force-recreate", "--no-deps", "owner", "sidecar"
    )

    assert _sidecar_netmode() == f"container:{_owner_id()}"
    _wait_healthy()
    assert _healthcheck_exit() == 0


def test_restarting_owner_alone_also_strands_the_sidecar_network(lifecycle):
    """Restarting the owner does NOT change its container ID, but Docker tears
    down the owner's netns on stop and creates a fresh one on start — the
    sidecar is left attached to the old netns and loses its eth0 interface.
    So restart is not a reliable recovery either: only recreating owner +
    sidecar together rejoins them.
    """
    _compose(
        lifecycle, PROJECT, "up", "-d", "--force-recreate", "--no-deps", "owner", "sidecar"
    )
    _wait_healthy()
    owner_before = _owner_id()

    _compose(lifecycle, PROJECT, "restart", "owner")

    assert _owner_id() == owner_before, "restart must not change the owner id"
    eth0 = subprocess.run(
        ["docker", "exec", f"{PROJECT}-sidecar-1", "sh", "-c", "ip addr | grep 'eth0'"],
        capture_output=True,
        text=True,
    )
    assert eth0.returncode != 0, "sidecar must have lost its network interface after owner restart"

    # Recovery is the same compose-level operation: recreate both together.
    _compose(
        lifecycle, PROJECT, "up", "-d", "--force-recreate", "--no-deps", "owner", "sidecar"
    )
    _wait_healthy()
    assert _healthcheck_exit() == 0
