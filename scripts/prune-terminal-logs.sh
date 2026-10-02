#!/bin/sh
# Retention for MT5 terminal and tester-agent logs.
#
# These are not the API's own logs (rotate-logs.sh handles those). They are
# written by terminal64.exe and metatester64.exe inside each terminal
# directory, in UTF-16, and nothing prunes them: MT5 has no retention setting
# and the log-rotator sidecar only ever saw data/shared/logs.
#
# Two controls, because age alone is not enough. A high-frequency strategy
# logs every order placement, cancellation and modification, so one backtest
# can produce tens of gigabytes in a single day — a 7-day window would let a
# disk fill long before the first prune fired. Age bounds slow creep; the size
# cap bounds the actual hazard.
#
#   RETAIN_DAYS    delete *.log older than this
#   MAX_LOG_BYTES  truncate any single *.log larger than this
#   IDLE_MINUTES   never touch a file written more recently than this, so a
#                  running backtest never loses its own diagnostics
#
# Truncate-in-place (: >) rather than delete for the size cap: the terminal
# holds these files open, so removing the inode would leave the writer pointed
# at a deleted file and the space would not come back until it exited.

set -eu

TERMINAL_ROOTS="${TERMINAL_ROOTS:-/terminals}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
MAX_LOG_BYTES="${MAX_LOG_BYTES:-2147483648}"
IDLE_MINUTES="${IDLE_MINUTES:-30}"
INTERVAL="${INTERVAL:-3600}"
DRY_RUN="${DRY_RUN:-0}"

log() {
    printf '[%s] [logprune] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

human() {
    awk -v b="$1" 'BEGIN {
        split("B KB MB GB TB", u, " "); i = 1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        printf "%.1f%s", b, u[i]
    }'
}

# Every terminal log directory: <terminal>/logs, <terminal>/Tester/logs and
# <terminal>/Tester/Agent-*/logs. Matching on the path keeps us off the rest
# of the terminal tree (Bases, MQL5, Reports) entirely.
find_logs() {
    root="$1"
    shift
    [ -d "$root" ] || return 0
    find "$root" \
        \( -path '*/Tester/logs/*.log' -o -path '*/Tester/Agent-*/logs/*.log' \) \
        -type f "$@" 2>/dev/null || true
}

prune_once() {
    for root in $(echo "$TERMINAL_ROOTS" | tr ':' ' '); do
        [ -d "$root" ] || {
            log "root missing, skipping: $root"
            continue
        }

        # 1. Age: delete whole files past the retention window.
        find_logs "$root" -mtime "+${RETAIN_DAYS}" | while read -r f; do
            size=$(wc -c <"$f" 2>/dev/null || echo 0)
            if [ "$DRY_RUN" = "1" ]; then
                log "would delete (age) $(human "$size") $f"
            elif rm -f "$f"; then
                log "deleted (age) $(human "$size") $f"
            fi
        done

        # 2. Size: truncate oversized files that nothing is actively writing.
        find_logs "$root" -size "+$((MAX_LOG_BYTES / 1024))k" | while read -r f; do
            size=$(wc -c <"$f" 2>/dev/null || echo 0)
            [ "$size" -gt "$MAX_LOG_BYTES" ] || continue

            # Skip anything touched inside the idle window — that is a live run.
            if [ -n "$(find "$f" -mmin "-${IDLE_MINUTES}" 2>/dev/null)" ]; then
                log "skipped (active, $(human "$size")) $f"
                continue
            fi

            if [ "$DRY_RUN" = "1" ]; then
                log "would truncate $(human "$size") $f"
            elif : >"$f"; then
                log "truncated $(human "$size") $f"
            fi
        done
    done

    # Per-file actions are logged above; each `find | while read` runs in its
    # own subshell, so totals cannot be accumulated here without a temp file.
    log "pass complete"
}

log "starting (roots=$TERMINAL_ROOTS retain_days=$RETAIN_DAYS max_log=$(human "$MAX_LOG_BYTES") idle_min=$IDLE_MINUTES interval=${INTERVAL}s dry_run=$DRY_RUN)"

if [ "${RUN_ONCE:-0}" = "1" ]; then
    prune_once
    exit 0
fi

while true; do
    if ! prune_once; then
        log "prune_once failed (continuing)"
    fi
    sleep "$INTERVAL"
done
