# Known issues

Defects that are understood and reproducible but not yet fixed. Committed
here (rather than filed externally) because Bitbucket Cloud's native Issue
Tracker API is deprecated (`CHANGE-3071`) and this repo has no other issue
tracker wired up. Move to whatever tracker replaces it if one gets adopted.

## Health monitor can be starved by the exact condition it exists to detect

**Found:** 2026-08-23, investigating a production incident where the mt5-b
VM's task queue pinned at depth 66 for over an hour across every broker
instance, with `/ping` responses taking 30-60+ minutes, requiring a full
`recreate-vm.sh mt5-b` to recover. No error path or crash — the monitor
just never acted.

**Root cause:** `mt5api/monitor.py`'s `_monitor_loop()` (the background
thread that's supposed to detect a dead/wedged terminal and call
`restart_terminal()`) does its own liveness check through
`with session():` — the *same* lock-acquisition/backpressure path every
client request goes through (`mt5client.py`'s `session()`,
`MAX_QUEUE_DEPTH`-gated). `session()` raises `QueueFull` when the queue is
already saturated. The monitor's outer exception handler is:

```python
except Exception as e:
    # session() backpressure (QueueFull) etc — surface and retry.
    log.warning("Monitor session failed: %s", e)
    continue
```

`QueueFull` is a plain `Exception` subclass, so it's caught here — and
`continue` skips straight back to the top of the loop **without touching
`dead_count`**. The monitor only escalates toward `restart_terminal()`
after `DEAD_CHECKS_BEFORE_RESTART` (5) consecutive checks where
`mt5.terminal_info` itself returns `None` or times out — a path that
requires the monitor to actually *acquire* the session first. Under real
backpressure, it never gets that far: every 60s (`CHECK_INTERVAL`) it logs
one `Monitor session failed: queue depth N exceeds max M` warning and
loops, forever, with no escalation, no priority bypass, and no alternate
signal ever reaching `dead_count`.

In short: **the fire alarm requires the building not to be on fire.** The
one condition the monitor exists to recover from — a terminal wedged badly
enough to saturate the request queue — is the one condition that prevents
the monitor's own check from running.

**Contributing factor (documented, not itself the bug):** `operations.md`
already describes the underlying wedge behavior as expected — "the
orphaned C-thread is still spinning inside the SDK" after a `mt5.*` call
times out (`mt5client.py`'s `_run_with_timeout()` spawns a
`threading.Thread(daemon=True)` per call with no cancellation; a
genuinely-hung SDK call leaves it running forever, still touching the
single non-thread-safe SDK connection). That accumulation is what drives
the queue deep enough, over enough repeated attempts, to keep `QueueFull`
firing indefinitely — but the monitor's inability to see past its own
backpressure gate is the separate, undocumented defect that turns a
recoverable wedge into a multi-hour outage requiring manual VM recreation.

**Suggested fix:** give the monitor's liveness probe a path that isn't
subject to `MAX_QUEUE_DEPTH` admission control — e.g. a dedicated
lower-priority-but-never-rejected lock acquisition (bypass the depth check
specifically for the monitor's own `session()` call, since it's a single
low-frequency internal caller, not client load), or at minimum treat
`QueueFull` from the monitor's own check as equivalent to a dead
check (increment `dead_count`) rather than silently retrying forever.
Either change makes the existing `DEAD_CHECKS_BEFORE_RESTART` mechanism
actually reachable under the exact conditions it was built for.

**This is tested, deliberately, as the current bug:**
`tests/test_monitor.py::test_monitor_loop_survives_a_session_failure_and_keeps_looping`
exists specifically to pin this behavior — its docstring says "the loop
must log and retry, never crash the monitor thread," and it asserts
exactly one `warning` log call with no crash. That's a reasonable goal in
isolation (the monitor shouldn't itself become a point of failure), but it
conflates "don't crash on backpressure" with "don't let backpressure count
as evidence of a dead terminal" — which is the actual defect. Fixing this
means changing what this test asserts: not just "survives and keeps
logging," but "repeated `QueueFull` eventually reaches
`restart_terminal()` the same way repeated `None`/timeout does."

Separately, `tests/test_mt5client.py::test_run_with_timeout_raises_mt5timeout_when_function_wedges`
covers timeout-detection but never asserts the spawned thread itself is
cleaned up — worth a regression test if `_run_with_timeout()` ever gets a
cancellable timeout instead of an abandoned daemon thread.
