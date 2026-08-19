#!/usr/bin/env python3
"""Healthcheck for the wickworks TA sidecar.

The sidecar shares the mt5 VM container's network namespace via compose's
``network_mode: service:mt5``. Docker resolves that namespace once, at
container start. When the mt5 container is later recreated (not merely
restarted), Docker gives the mt5 container a FRESH network namespace and a
fresh container ID, and leaves this sidecar running in the old, now-orphaned
one. The sidecar's own health endpoint on loopback still answers, so the
image's built-in healthcheck stays green while the Windows VM can no longer
reach wickworks at all — every ``/rates/ta`` call then fails with
``connection refused`` and the API surfaces a 502.

This healthcheck therefore reports whether the sidecar is still attached to a
LIVE mt5 netns, beyond what the image's loopback probe knows:

- It confirms the sidecar is still attached to a LIVE mt5 netns by probing
  the dockurr gateway services (20.20.20.1:445/139/5900/5700) that only
  exist while sharing the current mt5 netns. When the mt5 container is
  recreated, those services move to the new netns and become unreachable
  here, which is the earliest detectable sign of orphaning. The probes run
  concurrently so the all-unreachable (orphan) path is bounded by one probe
  timeout, keeping the whole check inside Docker's healthcheck timeout.

It is deliberately DETECTION ONLY — it never kills the sidecar process. A
healthcheck inside a stale namespace cannot repair its own immutable
``NetworkMode=container:<old-owner-id>`` binding: killing the process makes
compose restart the sidecar, and Docker refuses that restart ("cannot join
network namespace of container ... is restarting / No such container"). The
sidecar would exit permanently instead of staying up as a healthy-loopback /
orphaned diagnostic. Recovery is a Compose-level lifecycle operation:

- Recreating the owner VM together with its sidecar
  (``docker compose up -d --force-recreate <vm> <wickworks>``) gives the
  sidecar a fresh ``NetworkMode=container:<new-owner-id>``. This is the
  reliable operation (verified: ``depends_on`` does NOT rejoin sidecars on
  owner recreate, and restarting the owner alone also strands the sidecar's
  network in this runtime).
- ``./scripts/recreate-vm.sh <vm>`` wraps that compose command and discovers
  each VM's sidecars from the generated docker-compose.yml.

Exit codes: 0 = healthy (still sharing the live mt5 netns), 1 = unhealthy
(service itself down, or orphaned from the mt5 netns).
"""

import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

# dockurr gateway services that only exist while sharing the LIVE mt5 netns.
# The SMB/VNC/dockurr ports are bound by the mt5 container's own processes,
# so they are present when the namespaces are shared and gone when orphaned.
GATEWAY_HOST = os.environ.get("WICKWORKS_GATEWAY_HOST", "20.20.20.1")
GATEWAY_PORTS = [
    int(p) for p in os.environ.get("WICKWORKS_GATEWAY_PORTS", "445,139,5900,5700").split(",") if p.strip()
]
PROBE_TIMEOUT = int(os.environ.get("WICKWORKS_PROBE_TIMEOUT", "2"))

# Loopback health endpoint of the wickworks service itself.
SELF_HEALTH_URL = os.environ.get("WICKWORKS_SELF_HEALTH_URL", "http://127.0.0.1:8000/health")


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
    window. Parallel probing bounds the sweep at one ``PROBE_TIMEOUT``
    regardless of how many ports there are.
    """
    with ThreadPoolExecutor(max_workers=len(GATEWAY_PORTS)) as pool:
        return any(pool.map(_probe, GATEWAY_PORTS))


def main():
    if not _self_healthy():
        # The service itself is down — unhealthy. No restart attempt: a dead
        # uvicorn already causes the container to stop on its own.
        return 1
    if _shares_live_mt5_netns():
        # Still inside the live mt5 netns — normal healthy state.
        return 0
    # Orphaned: the mt5 container was recreated under a new netns. Report
    # unhealthy so operators can see it. Do NOT kill the process — a healthcheck
    # cannot repair its own immutable NetworkMode binding, and killing the
    # sidecar makes compose restart it into a netns that no longer exists.
    # Recovery: recreate the VM together with its sidecar, or restart the VM
    # (owner ID unchanged keeps this sidecar attached).
    print(
        "wickworks orphaned from the live mt5 netns; "
        "recreate the VM together with this sidecar to rejoin",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
