#!/usr/bin/env python3
"""Healthcheck for the wickworks TA sidecar.

The sidecar shares the mt5 VM container's network namespace via compose's
``network_mode: service:mt5``. Docker resolves that namespace once, at
container start. When the mt5 container is later restarted or recreated (a
VM reboot, a manual ``docker restart mt5``, a compose recreate), Docker gives
the mt5 container a FRESH network namespace and leaves this sidecar running
in the old, now-orphaned one. The sidecar's own health endpoint on loopback
still answers, so the image's built-in healthcheck stays green while the
Windows VM can no longer reach wickworks at all — every ``/rates/ta`` call
then fails with ``connection refused`` and the API surfaces a 502.

This healthcheck therefore does two things beyond the image's loopback probe:

1. It confirms the sidecar is still attached to a LIVE mt5 netns by probing
   the dockurr gateway services (20.20.20.1:445/139/5900/5700) that only
   exist while sharing the current mt5 netns. When the mt5 container is
   recreated, those services move to the new netns and become unreachable
   here, which is the earliest detectable sign of orphaning. The probes run
   concurrently so the all-unreachable (orphan) path is bounded by one probe
   timeout, keeping the whole check inside Docker's healthcheck timeout.

2. When orphaning is detected it kills the container's main uvicorn process
   (PID 1 is ``sh``; ``kill 1`` from an exec'd healthcheck is not delivered,
   but killing the uvicorn child makes ``sh`` exit cleanly), so the compose
   ``restart: unless-stopped`` policy recreates the container and it rejoins
   the current mt5 netns.

Exit codes: 0 = healthy, 1 = unhealthy. When unhealthy due to orphaning the
process also terminates itself so the restart policy can actually fire.
"""

import os
import signal
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

# dockurr gateway services that only exist while sharing the LIVE mt5 netns.
# The SMB/VNC/dockurr ports are bound by the mt5 container's own processes,
# so they are present when the namespaces are shared and gone when orphaned.
GATEWAY_HOST = os.environ.get("WICKWORKS_GATEWAY_HOST", "20.20.20.1")
GATEWAY_PORTS = [445, 139, 5900, 5700]
PROBE_TIMEOUT = 2

# Loopback health endpoint of the wickworks service itself.
SELF_HEALTH_URL = "http://127.0.0.1:8000/health"


def _self_healthy():
    """True when wickworks answers its own health endpoint on loopback."""
    import urllib.request

    try:
        with urllib.request.urlopen(SELF_HEALTH_URL, timeout=PROBE_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def _probe(port):
    """One gateway probe. Returns True when the port answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(PROBE_TIMEOUT)
    try:
        return sock.connect_ex((GATEWAY_HOST, port)) == 0
    finally:
        sock.close()


def _shares_live_mt5_netns():
    """True when any dockurr gateway service answers through the shared netns.

    These services are owned by the mt5 container's processes. When the mt5
    container is recreated, they live in the new netns, so a refused/unrouted
    connection here means this sidecar has been orphaned.

    Probes run concurrently: a serial sweep would take up to
    ``len(GATEWAY_PORTS) * PROBE_TIMEOUT`` seconds when every port is down,
    and Docker kills this healthcheck after its compose ``timeout`` — so the
    orphan path (where every probe fails) must complete well within that
    window or the self-heal never fires. Parallel probing bounds the sweep at
    one ``PROBE_TIMEOUT`` regardless of how many ports there are.
    """
    with ThreadPoolExecutor(max_workers=len(GATEWAY_PORTS)) as pool:
        return any(pool.map(_probe, GATEWAY_PORTS))


def _kill_main_process():
    """Kill the uvicorn process so PID 1 (sh) exits and the container stops.

    ``kill 1`` from a Docker exec'd healthcheck is not delivered to the
    container's init in this runtime, so targeting the uvicorn child (the
    process whose death makes ``sh -c uvicorn ...`` return) is what actually
    terminates the container. The ``sh -c`` wrapper is deliberately skipped:
    its cmdline also contains ``uvicorn``, and signalling it is the one thing
    that does not work.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or entry == "1":
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        if "uvicorn" in cmdline and "--multiprocessing-fork" not in cmdline:
            try:
                os.kill(int(entry), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return
    # Fallback: no uvicorn found — kill whatever is PID 1 via the signal that
    # does get delivered from an exec'd process.
    try:
        os.kill(1, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def main():
    if not _self_healthy():
        # The service itself is down — unhealthy without trying to restart;
        # a dead uvicorn already causes the container to stop on its own.
        return 1
    if _shares_live_mt5_netns():
        # Still inside the live mt5 netns — normal healthy state.
        return 0
    # Orphaned: the mt5 container was recreated under a new netns. Kill the
    # main process so the restart policy recreates us into the current netns.
    print(
        "wickworks orphaned from the live mt5 netns; "
        "killing main process so the restart policy rejoins it",
        file=sys.stderr,
    )
    _kill_main_process()
    return 1


if __name__ == "__main__":
    sys.exit(main())
