"""Integration test: a REAL watchdog recovery, end to end, on a disposable
Compose project.

psyb0t's blocker on PR #15 (2026-09-04): the watchdog container is handed
WATCHDOG_PROJECT_DIR but never MT5_PROJECT_DIR, while docker-compose.yml
requires `${MT5_PROJECT_DIR:?}`. So recreate-vm.sh's `docker compose` failed at
interpolation and no real (non-dry-run) recovery could ever complete. The unit
suite could not see it because nothing there ran the real child environment
against real compose. This does, with nothing faked in the chain:

  the sidecar image built from Dockerfile.watchdog, the real vm-watchdog.py,
  the real recreate-vm.sh, the real `docker compose`, the real Docker daemon.

It stands up a fake "VM" whose healthcheck fails once /tmp/unhealthy exists, a
`network_mode: service:vm` sidecar, and the watchdog with the Docker socket
mounted. MT5_PROJECT_DIR is supplied ONLY to this test's own `up` - as an
operator's shell would - and deliberately NOT passed into the watchdog
container, reproducing the production environment psyb0t described. Then it:

  1. proves the blocker is real INSIDE that container: `docker compose config`
     there fails with compose's own "required variable MT5_PROJECT_DIR" error;
  2. makes the VM unhealthy and waits for the watchdog to recover it;
  3. asserts a NEW VM container exists, the sidecar's NetworkMode names it, the
     sidecar has an eth0 again, and the watchdog recorded exactly one attempt.

Runs on the host (`make test-integration`), like test_wickworks_lifecycle.py.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .host_shared_fixture import create_host_shared_fixture_dir

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = "vm-watchdog-lifecycle-test"
VM_IMAGE = "python:3.12-alpine"
SIDECAR_IMAGE = "alpine:3.20"

RECOVERY_TIMEOUT_SECONDS = 150
STARTUP_TIMEOUT_SECONDS = 90


def _require_compose():
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose not available")


def _write_project(project_dir: Path) -> None:
    """A compose project shaped like production where it matters.

    The watchdog service copies the shipped one: socket mount, script mount,
    the project mounted at the SAME absolute path through the `${MT5_PROJECT_DIR:?}`
    interpolation, WATCHDOG_* set, and MT5_PROJECT_DIR conspicuously NOT in its
    environment. Timings are shortened so the test finishes in seconds, and
    the image filter names the fake VM's image.
    """
    scripts = project_dir / "scripts"
    scripts.mkdir()
    helper = scripts / "recreate-vm.sh"
    shutil.copy(REPO_ROOT / "scripts" / "recreate-vm.sh", helper)
    helper.chmod(0o755)

    (project_dir / "docker-compose.yml").write_text(
        f"""services:
  vm:
    image: {VM_IMAGE}
    command: ["python3", "-c", "import time\\nwhile True: time.sleep(1)"]
    healthcheck:
      test: ["CMD", "sh", "-c", "test ! -f /tmp/unhealthy"]
      interval: 1s
      timeout: 1s
      retries: 1
      start_period: 0s
    stop_grace_period: 3s

  sidecar:
    image: {SIDECAR_IMAGE}
    command: ["sleep", "infinity"]
    network_mode: "service:vm"
    # An ORPHAN-AWARE healthcheck, which is what the watchdog's sidecar sweep
    # requires and what scripts/wickworks-healthcheck.py does for real (it
    # probes the owner's gateway services). A check that only looked at itself
    # would stay green inside a dead namespace, which is exactly how the
    # 2026-09-07 fault hid for two days. Losing eth0 is the same signal here.
    healthcheck:
      test: ["CMD", "sh", "-c", "test -e /sys/class/net/eth0"]
      interval: 1s
      timeout: 1s
      retries: 1
      start_period: 0s
    stop_grace_period: 3s
    depends_on:
      - vm

  vm-watchdog:
    build:
      context: {REPO_ROOT}
      dockerfile: Dockerfile.watchdog
    command: ["python", "-u", "/vm-watchdog.py"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - {REPO_ROOT}/scripts/vm-watchdog.py:/vm-watchdog.py:ro
      - ${{MT5_PROJECT_DIR:?MT5_PROJECT_DIR must be the absolute host path of this project}}:${{MT5_PROJECT_DIR}}:ro
    tmpfs:
      - /state
    environment:
      WATCHDOG_STATE_DIR: /state
      WATCHDOG_PROJECT_DIR: {project_dir}
      WATCHDOG_RECREATE_SCRIPT: {project_dir}/scripts/recreate-vm.sh
      WATCHDOG_COMPOSE_PROJECT: {PROJECT}
      WATCHDOG_IMAGE_FILTER: python
      WATCHDOG_WATCH_SIDECARS: "1"
      WATCHDOG_MIN_FAILING_STREAK: "3"
      WATCHDOG_INTERVAL_SECONDS: "1"
      WATCHDOG_BACKOFF_ATTEMPTS: "5"
      WATCHDOG_MAX_ATTEMPTS: "3"
      WATCHDOG_RESET_SECONDS: "60"
      RECREATE_STOP_TIMEOUT: "3"
      # Deliberately absent: MT5_PROJECT_DIR. The watchdog must hand it to the
      # helper itself; the container is not given it.
""",
        encoding="utf-8",
    )


def _compose(project_dir: Path, *args, env_extra=None, check=True):
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(project_dir / "docker-compose.yml"), *args]
    # A clean environment: only the test's own `up` gets MT5_PROJECT_DIR, and
    # nothing here inherits one a developer happens to have exported.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    if "DOCKER_HOST" in os.environ:
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    env.update(env_extra or {})
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if check and result.returncode != 0:
        raise AssertionError(
            f"{' '.join(cmd)} failed (rc={result.returncode})\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _name(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def _inspect(service: str, tmpl: str) -> str:
    result = subprocess.run(["docker", "inspect", "--format", tmpl, _name(service)], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"docker inspect {_name(service)} failed: {result.stderr}")
    return result.stdout.strip()


def _exec(service: str, *cmd: str):
    return subprocess.run(["docker", "exec", _name(service), *cmd], capture_output=True, text=True)


def _watchdog_logs() -> str:
    return subprocess.run(["docker", "logs", _name("vm-watchdog")], capture_output=True, text=True).stdout


def _wait(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(1)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}\n--- watchdog log ---\n{_watchdog_logs()}")


@pytest.fixture(scope="module")
def stack():
    _require_compose()
    project_dir = create_host_shared_fixture_dir(REPO_ROOT, "vm-watchdog-lifecycle")
    try:
        _write_project(project_dir)
        # The operator's shell: MT5_PROJECT_DIR exported for THIS command only.
        _compose(project_dir, "up", "-d", "--build", env_extra={"MT5_PROJECT_DIR": str(project_dir)})
        _wait(lambda: _inspect("vm", "{{.State.Health.Status}}") == "healthy", STARTUP_TIMEOUT_SECONDS, "vm healthy")
        _wait(lambda: "scoped to compose project" in _watchdog_logs(), STARTUP_TIMEOUT_SECONDS, "watchdog started")
        yield project_dir
    finally:
        # The operator's shell again: `down` interpolates the compose file too,
        # so it needs MT5_PROJECT_DIR exactly as `make down` after run.sh does
        # (psyb0t's finding #2, in miniature - without it this teardown failed
        # at interpolation and, being check=False, silently left a
        # socket-mounted watchdog running). Loud now: a leaked privileged
        # container is worse than a noisy teardown.
        # --rmi local: the per-project build tag would otherwise accumulate one
        # dangling watchdog image per run.
        #
        # The watchdog is stopped FIRST, and this is not tidiness. It holds the
        # Docker socket and acts once a second; `down` removes containers one
        # at a time, so a sweep landing between the sidecar's removal and the
        # watchdog's own recreates the sidecar behind compose's back and leaves
        # it running after the project is gone. Observed, once, exactly that
        # way. Best-effort: if this fails, `down` below still has to run.
        _compose(
            project_dir, "stop", "-t", "3", "vm-watchdog",
            env_extra={"MT5_PROJECT_DIR": str(project_dir)}, check=False,
        )
        _compose(
            project_dir, "down", "-v", "--remove-orphans", "--rmi", "local",
            env_extra={"MT5_PROJECT_DIR": str(project_dir)},
        )
        shutil.rmtree(project_dir)


def test_the_watchdog_starts_clean_against_the_real_wiring(stack):
    """validate_config() passed: the project is mounted at the host path, the
    helper is where WATCHDOG_RECREATE_SCRIPT says, no problems were reported."""
    log = _watchdog_logs()
    assert "invalid configuration" not in log
    assert f"scoped to compose project '{PROJECT}'" in log


def test_the_blocker_is_real_inside_the_sidecar(stack):
    """Reproduce psyb0t's finding where it bites: compose run INSIDE the
    watchdog container, with the container's own environment, fails to even
    parse the project file. This is what every pre-fix recovery hit."""
    # The INNER command's exit code is echoed so the failure is provably the
    # inner compose (inside the container), not the outer `exec` wrapper.
    res = _compose(stack, "exec", "-T", "vm-watchdog", "sh", "-c",
                   f"docker compose -f {stack}/docker-compose.yml config --quiet; echo INNER_RC=$?", check=False)
    assert res.returncode == 0, f"outer exec failed, so nothing was tested: {res.stderr}"
    assert "INNER_RC=" in res.stdout and "INNER_RC=0" not in res.stdout, res.stdout
    assert "required variable MT5_PROJECT_DIR" in (res.stderr + res.stdout), res.stderr


def test_the_watchdog_gives_the_helper_what_the_container_never_got(stack):
    """The fix, observed at the boundary: recreate_env() built inside the real
    sidecar carries the project dir even though the container's env does not."""
    res = _compose(stack, "exec", "-T", "vm-watchdog", "python", "-c",
                   "import importlib.util, os\n"
                   "spec = importlib.util.spec_from_file_location('wd', '/vm-watchdog.py')\n"
                   "wd = importlib.util.module_from_spec(spec); spec.loader.exec_module(wd)\n"
                   "print('container-env:', os.environ.get('MT5_PROJECT_DIR', '<unset>'))\n"
                   f"print('helper-env:', wd.recreate_env('{PROJECT}')['MT5_PROJECT_DIR'])\n")
    assert "container-env: <unset>" in res.stdout, res.stdout
    assert f"helper-env: {stack}" in res.stdout, res.stdout


def test_a_persistently_unhealthy_vm_is_recreated_with_its_sidecar(stack):
    """The recovery, for real: make the VM unhealthy, wait for the watchdog,
    and check the world afterwards rather than the log alone."""
    vm_before = _inspect("vm", "{{.Id}}")
    assert _inspect("sidecar", "{{.HostConfig.NetworkMode}}") == f"container:{vm_before}"

    assert _exec("vm", "touch", "/tmp/unhealthy").returncode == 0

    def unhealthy_or_already_recreated():
        # The watchdog may win the race and replace the container between two
        # polls; inspect then fails on the vanished name. That is progress, not
        # an error - the recreate assertion below is what checks the outcome.
        try:
            return _inspect("vm", "{{.State.Health.Status}}") == "unhealthy"
        except AssertionError:
            return True

    _wait(unhealthy_or_already_recreated, 30, "vm to go unhealthy")

    _wait(
        lambda: "recreated vm and its sidecars" in _watchdog_logs(),
        RECOVERY_TIMEOUT_SECONDS,
        "the watchdog to recreate the vm",
    )
    log = _watchdog_logs()
    assert "recreate failed" not in log, log
    assert "MT5_PROJECT_DIR is missing" not in log, log

    vm_after = _inspect("vm", "{{.Id}}")
    assert vm_after != vm_before, "recovery must be a recreate, not a restart"
    # Fresh container filesystem: the poison file is gone, the VM comes back healthy.
    _wait(lambda: _inspect("vm", "{{.State.Health.Status}}") == "healthy", STARTUP_TIMEOUT_SECONDS, "recreated vm healthy")

    # The sidecar was recreated WITH it and rejoined the new netns.
    assert _inspect("sidecar", "{{.HostConfig.NetworkMode}}") == f"container:{vm_after}"
    interfaces = _exec("sidecar", "ls", "/sys/class/net").stdout.split()
    assert "eth0" in interfaces, f"sidecar has no eth0 after recovery: {interfaces}"

    # One attempt, recorded under the stable compose identity (not the old id).
    state = _compose(stack, "exec", "-T", "vm-watchdog", "cat", f"/state/{PROJECT}.vm.json").stdout
    assert '"attempts": 1' in state, state


def test_a_sidecar_stranded_by_an_owner_only_restart_is_rejoined_alone(stack):
    """The 2026-09-07 production fault, reproduced and then recovered.

    Restarting the OWNER alone is what `restart: unless-stopped` does after a
    clean guest shutdown, and it is the case no VM ever reports: the container
    id does not change, so the binding still names a running, healthy VM, while
    Docker has built it a brand new namespace and left the sidecar in the old
    one. Only the sidecar's own healthcheck can see that.

    What is asserted afterwards is the whole point of the change: the SIDECAR
    has a new container, bound to the same VM, with a working eth0, and the VM
    container was never touched.
    """
    vm_before = _inspect("vm", "{{.Id}}")
    sidecar_before = _inspect("sidecar", "{{.Id}}")
    _wait(lambda: _inspect("sidecar", "{{.State.Health.Status}}") == "healthy",
          STARTUP_TIMEOUT_SECONDS, "sidecar healthy to begin with")

    # Strand it, the way production did. Note: a RESTART, not a recreate.
    subprocess.run(["docker", "restart", "-t", "3", _name("vm")], capture_output=True, check=True)

    # The fault is real before the watchdog gets to it, and invisible from the
    # VM: same owner id, VM healthy, sidecar with no network.
    _wait(lambda: _inspect("vm", "{{.State.Health.Status}}") == "healthy",
          STARTUP_TIMEOUT_SECONDS, "the restarted vm healthy")
    assert _inspect("vm", "{{.Id}}") == vm_before, "a restart must not change the container id"
    assert _inspect("sidecar", "{{.Id}}") == sidecar_before, "the sidecar was not recreated"
    assert _inspect("sidecar", "{{.HostConfig.NetworkMode}}") == f"container:{vm_before}"
    assert "eth0" not in _exec("sidecar", "ls", "/sys/class/net").stdout.split()

    _wait(
        lambda: "recreated sidecar sidecar" in _watchdog_logs(),
        RECOVERY_TIMEOUT_SECONDS,
        "the watchdog to recreate the stranded sidecar",
    )
    log = _watchdog_logs()
    assert "sidecar recreate failed" not in log, log

    _wait(lambda: _inspect("sidecar", "{{.Id}}") != sidecar_before, 60, "a new sidecar container")
    interfaces = _exec("sidecar", "ls", "/sys/class/net").stdout.split()
    assert "eth0" in interfaces, f"sidecar still has no eth0: {interfaces}"
    _wait(lambda: _inspect("sidecar", "{{.State.Health.Status}}") == "healthy", 60, "sidecar healthy")

    # Only the sidecar. The VM the terminals live in was not restarted, not
    # recreated and not stopped.
    assert _inspect("vm", "{{.Id}}") == vm_before, "the vm must not have been touched"
    assert _inspect("vm", "{{.State.Health.Status}}") == "healthy"

    # Recorded under the sidecar's own compose identity, with its own budget.
    state = _compose(stack, "exec", "-T", "vm-watchdog", "cat", f"/state/{PROJECT}.sidecar.json").stdout
    assert '"attempts": 1' in state, state


def test_a_sidecar_under_a_stopped_owner_is_left_running(stack):
    """The counter-review's sharpest finding, pinned against a real daemon.

    `/containers/json` lists running containers only, so a stopped owner looks
    exactly like a destroyed one. Acting on that would run the helper, which
    stops the sidecar first and then cannot start it again - nothing to join -
    leaving it STOPPED and invisible to both sweeps forever. An operator
    stopping a VM for maintenance must not lose the sidecar with it.
    """
    sidecar_before = _inspect("sidecar", "{{.Id}}")
    subprocess.run(["docker", "stop", "-t", "3", _name("vm")], capture_output=True, check=True)
    try:
        marker = len(_watchdog_logs())
        # Several sweeps at WATCHDOG_INTERVAL_SECONDS=1, well past the streak.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert _inspect("sidecar", "{{.State.Status}}") == "running", (
                "the sidecar was stopped while its owner was merely stopped\n"
                + _watchdog_logs()[marker:]
            )
            time.sleep(1)
        assert _inspect("sidecar", "{{.Id}}") == sidecar_before
        assert "recreated sidecar" not in _watchdog_logs()[marker:]
    finally:
        # Starting the owner again gives it a FRESH namespace, which strands
        # the sidecar exactly as the previous test did - so hand the recreate
        # to compose here rather than leaving a live orphan for the watchdog to
        # act on while the fixture is tearing the project down.
        subprocess.run(["docker", "start", _name("vm")], capture_output=True, check=True)
        _wait(lambda: _inspect("vm", "{{.State.Health.Status}}") == "healthy",
              STARTUP_TIMEOUT_SECONDS, "the vm healthy again")
        _compose(stack, "up", "-d", "--force-recreate", "--no-deps", "sidecar",
                 env_extra={"MT5_PROJECT_DIR": str(stack)})
        _wait(lambda: _inspect("sidecar", "{{.State.Health.Status}}") == "healthy",
              STARTUP_TIMEOUT_SECONDS, "the sidecar healthy again")
