"""Give a backtest VM a fixed pagefile big enough for its testers' commit.

Windows can only hand out memory it can back with RAM plus pagefile: the
commit limit. MetaTrader's tester agents commit far more than they touch
(16 agents: 74.7 GB committed, ~20 GB in use), so a VM running many tests
reaches the limit long before its RAM is full. On 11 Oct 2026 the bulk VM
sat at 85.17 / 90.70 GB with 34 GB of RAM still free; tests failed with
"no memory for ticks generating" (exit 100013) and, before the API processes
stopped committing ~780 MB each, both VMs powered off when the limit ran out.

Pages that are committed but never touched are never written, so a larger
pagefile mostly raises the limit. The VM disks are sparse images on the
host's root SSD, which bounds how large it may safely be.

Run by start.bat at every boot. Exit codes:
    0  already at least the target, nothing changed
    3  set; takes effect on the next boot, so the caller reboots
    1  could not read or write the value
"""
from __future__ import annotations

import sys

KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
VALUE = "PagingFiles"
PAGEFILE = r"C:\pagefile.sys"
#: 40 GB: commit limit ~112 GB with the VMs' 72 GB of RAM.
TARGET_MB = 40960


def wanted_paging_files(current, target_mb=TARGET_MB):
    """The PagingFiles entries to write, or None when ``current`` is enough.

    Enough means a fixed C: pagefile whose initial and maximum size are both
    at least ``target_mb``. Anything else - Windows managing the size
    (``?:\\pagefile.sys``, or ``0 0``), a smaller file, no pagefile at all -
    is replaced with one fixed ``target_mb`` file on C:.
    """
    for entry in current or []:
        parts = entry.split()
        if len(parts) != 3 or parts[0].lower() != PAGEFILE.lower():
            continue
        try:
            initial, maximum = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if initial >= target_mb and maximum >= target_mb:
            return None
    return [f"{PAGEFILE} {target_mb} {target_mb}"]


def main():
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            KEY,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                current, _ = winreg.QueryValueEx(key, VALUE)
            except FileNotFoundError:
                current = []
            if isinstance(current, str):
                current = [current]
            wanted = wanted_paging_files(current)
            if wanted is None:
                print(f"pagefile ok (>= {TARGET_MB} MB)")
                return 0
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_MULTI_SZ, wanted)
            written, _ = winreg.QueryValueEx(key, VALUE)
    except OSError as exc:
        print(f"pagefile check failed: {exc}")
        return 1
    if wanted_paging_files(written) is not None:
        print("pagefile write did not stick")
        return 1
    print(f"pagefile set to {TARGET_MB} MB fixed (was {current!r}); reboot to apply")
    return 3


if __name__ == "__main__":
    sys.exit(main())
