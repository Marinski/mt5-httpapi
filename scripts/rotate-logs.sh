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

log() {
    printf '[%s] [rotator] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

is_positive_integer() {
    case "$1" in
    '' | *[!0-9]*) return 1 ;;
    esac

    [ "$1" -gt 0 ]
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
        fi
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

log "starting (log_dir=$LOG_DIR terminals_dir=$TERMINALS_DIR retain_days=$RETAIN_DAYS interval=${INTERVAL}s)"
while true; do
    if ! rotate_once; then
        log "rotate_once failed (continuing)"
    fi
    sleep "$INTERVAL"
done
