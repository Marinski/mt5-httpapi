#!/usr/bin/env bash
#
# recreate-vm.sh -- recreate a VM container together with its network_mode
# sidecars, so the sidecars rejoin the new netns.
#
# WHY THIS EXISTS
# ---------------
# A wickworks sidecar shares the VM's network namespace via compose's
# `network_mode: service:<vm>`. Docker resolves that namespace ONCE, at
# container start, into an immutable `NetworkMode=container:<owner-id>`.
# `docker compose up -d --force-recreate <vm>` gives the VM a NEW container
# ID and a fresh netns, but leaves the sidecar pointed at the deleted ID — it
# can no longer rejoin, and its own healthcheck has no way to repair the
# binding (see scripts/wickworks-healthcheck.py). Restarting the VM is not a
# reliable alternative either: the owner ID survives a restart, but Docker
# creates a fresh netns on start, so the sidecar still strands (verified in
# tests/integration/test_wickworks_lifecycle.py).
#
# The correct recreate operation names BOTH the VM and its sidecars:
#   docker compose up -d --force-recreate <vm> <wickworks>
# Compose then creates the sidecar with `NetworkMode=container:<new-owner-id>`.
# This script is that operation, for one or more VM services at a time, and
# discovers each VM's sidecars from the generated docker-compose.yml rather
# than hardcoding service names.
#
# USAGE
# -----
#   ./scripts/recreate-vm.sh <service> [<service>...]
#   ./scripts/recreate-vm.sh --dry-run <service> [<service>...]
#
# EXAMPLES
# --------
#   ./scripts/recreate-vm.sh mt5          # recreate mt5 + its sidecars
#   ./scripts/recreate-vm.sh mt5 mt5-b    # recreate both VMs + their sidecars
#
# STOPPING BEFORE RECREATING
# --------------------------
# `docker compose up --force-recreate` stops each container using compose's OWN
# --timeout, which defaults to 10 SECONDS, not the service's stop_grace_period.
# A dockurr/windows VM needs far longer than that to shut down (ours declare
# `stop_grace_period: 2m`), so compose gave up waiting and went straight to
# removing a container that was still running:
#
#   Error response from daemon: cannot remove container "...":
#   container is running: stop the container before removing or force remove
#
# The watchdog then recorded a failed recreate and backed off, leaving the VM
# unhealthy and its sidecars stranded — the exact outcome this script exists to
# prevent. So the targets are stopped explicitly first, with a timeout that
# matches the grace period, and the same value is passed to `up` so its implicit
# stop can never fall back to 10s.
#
# ENV
# ---
#   COMPOSE_FILE           compose file to read services from
#                          (default: ./docker-compose.yml in the repo root)
#   RECREATE_STOP_TIMEOUT  seconds to allow each container to stop
#                          (default: 120, matching stop_grace_period: 2m).
#                          Keep this below WATCHDOG_RECREATE_TIMEOUT (300s) or
#                          the watchdog kills the script mid-recreate.
#
# The script never touches services it was not asked to recreate, and never
# uses --no-deps in a way that skips the named sidecars.

set -euo pipefail

trap 'echo "[ERROR] ${BASH_SOURCE[0]}:${LINENO} - command failed (exit $?)" >&2' ERR

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${DIR}/docker-compose.yml}"
STOP_TIMEOUT="${RECREATE_STOP_TIMEOUT:-120}"
DRY_RUN=0

usage() {
    sed -n '2,12p' "${BASH_SOURCE[0]}"
    exit 1
}

log() {
    printf '[%s] [recreate-vm] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# Sidecars of a VM service: every service whose network_mode references it.
# Parsed from the generated compose file, because that is the single source
# of truth for what is actually deployed (vms.yaml may name more than the
# generated file; the template may also change over time).
vm_sidecars() {
    local vm="$1"
    python3 - "$COMPOSE_FILE" "$vm" <<'PY'
import sys

import yaml

path, vm = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

out = []
for name, svc in (data.get("services") or {}).items():
    if svc.get("network_mode") == f"service:{vm}":
        out.append(name)
for name in sorted(out):
    print(name)
PY
}

main() {
    if [ "${1:-}" = "--dry-run" ]; then
        DRY_RUN=1
        shift
    fi
    [ "$#" -ge 1 ] || usage
    [ -f "$COMPOSE_FILE" ] || {
        echo "ERROR: compose file not found at ${COMPOSE_FILE}" >&2
        exit 1
    }

    # Expand the requested services into a flat, de-duplicated recreate list:
    # the VM itself plus every sidecar that shares its netns.
    local recreate=()
    for vm in "$@"; do
        recreate+=("$vm")
        while IFS= read -r sidecar; do
            [ -n "$sidecar" ] && recreate+=("$sidecar")
        done < <(vm_sidecars "$vm")
    done
    # Deduplicate preserving order.
    local -a targets=()
    local s
    for s in "${recreate[@]}"; do
        local dup=0
        for t in "${targets[@]:-}"; do
            [ "$t" = "$s" ] && {
                dup=1
                break
            }
        done
        [ "$dup" = "0" ] && targets+=("$s")
    done

    if [ "$DRY_RUN" = "1" ]; then
        log "DRY-RUN: would run 'docker compose stop -t ${STOP_TIMEOUT} ${targets[*]}'"
        log "DRY-RUN: would run 'docker compose up -d --force-recreate --no-deps -t ${STOP_TIMEOUT} ${targets[*]}'"
        return 0
    fi

    # Stop first, with the real grace period. See STOPPING BEFORE RECREATING.
    log "stopping (timeout ${STOP_TIMEOUT}s): ${targets[*]}"
    docker compose -f "$COMPOSE_FILE" stop -t "$STOP_TIMEOUT" "${targets[@]}"

    log "recreating: ${targets[*]}"
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate --no-deps -t "$STOP_TIMEOUT" "${targets[@]}"
    log "recreate done"
}

main "$@"
