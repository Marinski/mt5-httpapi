#!/bin/sh
# Daily rotation for logs in $LOG_DIR. Idempotent: keyed on whether
# yesterday's archive already exists, so re-runs are no-ops. Runs as a
# loop inside an alpine sidecar; no cron daemon needed.
#
# Truncate-in-place (cp + : >) instead of mv: full.log is held open by
# the Python API's FileHandler, so renaming the inode would leave the
# writer pointed at the renamed file forever. Truncating preserves the
# inode — Python keeps writing, the file just appears empty on next
# append. cmd.exe `>>` and PowerShell `Add-Content` reopen per write so
# either approach works for them.

set -eu

LOG_DIR="${LOG_DIR:-/logs}"
TERMINALS_DIR="${TERMINALS_DIR:-/terminals}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
INTERVAL="${INTERVAL:-3600}"
# Terminal journals need a size bound as well as an age one — see
# cap_journal_size. IDLE_MINUTES is the "a backtest is still writing this"
# guard; nothing touched more recently is truncated.
MAX_LOG_BYTES="${MAX_LOG_BYTES:-2147483648}"
IDLE_MINUTES="${IDLE_MINUTES:-30}"

log() {
    printf '[%s] [rotator] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

is_positive_integer() {
    case "$1" in
    '' | *[!0-9]*) return 1 ;;
    esac

    [ "$1" -gt 0 ]
}

# Age alone cannot bound these. A high-frequency strategy logs every order
# placement, modification and cancellation, so a single backtest can write tens
# of gigabytes into TODAY's journal — which the retention window deliberately
# will not touch for RETAIN_DAYS, long after the disk has filled.
#
# This reclaims that space once the run goes quiet; it does NOT bound a journal
# while its own backtest is still writing (see IDLE_MINUTES below), because
# truncating a running job's log destroys the diagnostics for the very run
# producing them.
#
# Truncate in place rather than delete: the terminal holds the journal open, so
# unlinking the inode would leave the writer pointed at a deleted file and the
# space would not come back until the terminal exited.
cap_journal_size() {
    journal=$1

    # stat, not `wc -c`: busybox wc READS the whole file to count bytes, which
    # costs ~18s on a 3 GB journal (measured in alpine:3.20) and would run for
    # every in-window journal every INTERVAL — on exactly the multi-gigabyte
    # files this function exists for. stat is one fstat on busybox and GNU
    # alike. The test image has GNU coreutils, where `wc -c` is already O(1),
    # so this cost is invisible to the suite and only appears in production.
    size=$(stat -c %s "$journal" 2>/dev/null || echo 0)
    [ "$size" -gt "$MAX_LOG_BYTES" ] || return 0

    # Anything written inside the idle window belongs to a running backtest;
    # truncating it would destroy the diagnostics for the very run producing
    # them. Let it exceed the cap until it goes quiet.
    if [ -n "$(find "$journal" -mmin "-${IDLE_MINUTES}" 2>/dev/null)" ]; then
        log "over cap but still active, left alone (${size}B) $journal"
        return 0
    fi

    # Restore the mtime afterwards. _tail_dir_log picks the newest .log in a
    # directory by mtime, so bumping this one to now would make a just-emptied
    # journal outrank the journal a running job is actually writing, and
    # GET /backtest/<id>/tail would answer with nothing until that job's next
    # write. Truncating is this script's housekeeping, not the terminal
    # logging, so it should not look like the most recent activity.
    mtime=$(stat -c %Y "$journal" 2>/dev/null || echo "")
    if : >"$journal"; then
        if [ -n "$mtime" ]; then
            touch -d "@$mtime" "$journal" 2>/dev/null || true
        fi
        log "truncated oversized terminal journal (${size}B) $journal"
    fi
}

prune_journal_dir() {
    journal_dir=$1
    cutoff=$2

    [ -d "$journal_dir" ] || return 0

    for journal in "$journal_dir"/????????.log; do
        [ -f "$journal" ] || continue
        journal_date="${journal##*/}"
        journal_date="${journal_date%.log}"
        case "$journal_date" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
        *) continue ;;
        esac

        if [ "$journal_date" -lt "$cutoff" ]; then
            rm -f "$journal"
            log "pruned terminal journal $journal"
            continue
        fi

        # Still inside the retention window — bound it by size instead.
        cap_journal_size "$journal"
    done
}

prune_terminal_journals() {
    cutoff=$1

    [ -d "$TERMINALS_DIR" ] || return 0

    find "$TERMINALS_DIR" -type f -name terminal64.exe -print |
        while IFS= read -r terminal_binary; do
            terminal_dir="${terminal_binary%/terminal64.exe}"
            prune_journal_dir "$terminal_dir/logs" "$cutoff"
            prune_journal_dir "$terminal_dir/Tester/logs" "$cutoff"

            for agent_journal_dir in "$terminal_dir"/Tester/Agent-*/logs; do
                prune_journal_dir "$agent_journal_dir" "$cutoff"
            done
        done
}

rotate_once() {
    now=$(date -u +%s)
    yesterday=$(date -u -d "@$((now - 86400))" +%Y%m%d)
    cutoff=$(date -u -d "@$((now - RETAIN_DAYS * 86400))" +%Y%m%d)

    for f in "$LOG_DIR"/*.log; do
        [ -f "$f" ] || continue
        archive="${f}.${yesterday}"
        [ -e "$archive" ] && continue
        [ -s "$f" ] || continue
        # Atomic: cp to .tmp then mv. If cp fails (disk full etc.) the
        # partial sits as .tmp and gets retried/overwritten next cycle —
        # never leaves a half-written archive blocking rotation.
        if cp "$f" "${archive}.tmp" && mv "${archive}.tmp" "$archive"; then
            : >"$f"
            log "rotated $(basename "$f") -> $(basename "$archive")"
        else
            rm -f "${archive}.tmp"
            log "rotation FAILED for $(basename "$f")"
        fi
    done

    # Prune *.log.YYYYMMDD older than cutoff. Lex sort == chrono sort
    # because the suffix is fixed-width YYYYMMDD.
    for old in "$LOG_DIR"/*.log.[0-9]*; do
        [ -f "$old" ] || continue
        suffix="${old##*.}"
        case "$suffix" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
        *) continue ;;
        esac
        if [ "$suffix" -lt "$cutoff" ]; then
            rm -f "$old"
            log "pruned $(basename "$old")"
        fi
    done

    prune_terminal_journals "$cutoff"
}

if ! is_positive_integer "$RETAIN_DAYS"; then
    log "RETAIN_DAYS must be a positive integer, got: $RETAIN_DAYS"
    exit 1
fi

if ! is_positive_integer "$MAX_LOG_BYTES"; then
    log "MAX_LOG_BYTES must be a positive integer, got: $MAX_LOG_BYTES"
    exit 1
fi

if ! is_positive_integer "$IDLE_MINUTES"; then
    log "IDLE_MINUTES must be a positive integer, got: $IDLE_MINUTES"
    exit 1
fi

log "starting (log_dir=$LOG_DIR terminals_dir=$TERMINALS_DIR retain_days=$RETAIN_DAYS max_log=${MAX_LOG_BYTES}B idle_min=$IDLE_MINUTES interval=${INTERVAL}s)"
while true; do
    if ! rotate_once; then
        log "rotate_once failed (continuing)"
    fi
    sleep "$INTERVAL"
done
