"""Worker process for the cross-process compile-lock test.

Deliberately its own module. `multiprocessing`'s "spawn" start method pickles
the target by reference and RE-IMPORTS its defining module in the fresh
interpreter — and `tests/test_compile.py` imports `mt5api.config` at module
level, which needs the MetaTrader5 stub installed first. Importing this module
pulls in nothing but the standard library, so the child can load it and then
install the stub itself before touching any mt5api code.
"""

import os
import sys
import time


def _handler():
    sys.path.insert(0, os.environ["MT5_REPO_ROOT"])
    from tests.conftest import _install_mt5_stub

    _install_mt5_stub()
    from mt5api.handlers import compile as handler

    return handler


def run(hold_seconds, barrier, results, die_holding=False):
    """Acquire the cross-process compile lock, hold it, and record the exact
    window during which this process believed it owned it.

    With `die_holding`, exit the interpreter while holding the lock and
    without releasing it, as a killed API process would.
    """
    handler = _handler()

    # timeout, not a bare wait(): if a sibling dies during import the rest must
    # fail the test rather than block forever and hang pytest at exit.
    barrier.wait(timeout=120)
    if not die_holding:
        # Let the dying worker take the lock first.
        time.sleep(0.2)
    lock = handler._acquire_cross_process_lock(time.monotonic() + 60)
    if lock is None:
        results.append(("timeout", 0.0, 0.0))
        return
    if die_holding:
        time.sleep(hold_seconds)
        os._exit(9)
    entered = time.time()
    time.sleep(hold_seconds)  # stands in for the MetaEditor subprocess
    exited = time.time()
    handler._release_cross_process_lock(lock)
    results.append((os.getpid(), entered, exited))
