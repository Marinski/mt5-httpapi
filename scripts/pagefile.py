"""Give a backtest VM a fixed pagefile big enough for its testers' commit.

Windows can only hand out memory it can back with RAM plus pagefile: the
commit limit. MetaTrader's tester agents commit far more than they touch
(16 agents: 74.7 GB committed, ~20 GB in use), so a VM running many tests
reaches the limit long before its RAM is full. On 11 Oct 2026 the bulk VM
sat at 85.17 / 90.70 GB with 34 GB of RAM still free; tests failed with
"no memory for ticks generating" (exit 100013) and, before the API processes
stopped committing ~780 MB each, both VMs powered off when the limit ran out.

Pages that are committed but never touched are never written, so a larger
pagefile mostly raises the limit. The system disk is a sparse image on the
host's root SSD, which bounds how large a C: pagefile may safely be, so with
a pagefile disk attached (P:, see pagefile-disk.ps1) the large file goes
there and C: keeps a small one.

Run by start.bat at every boot. Exit codes:
    0  already at least the target, nothing changed
    3  set; takes effect on the next boot, so the caller reboots
    1  could not read or write the value
"""
from __future__ import annotations

import os
import sys

KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
VALUE = "PagingFiles"
#: With the pagefile disk (P:, an image on the host's /data drive, see
#: pagefile-disk.ps1): a small C: file for crash dumps and 120 GB on P:, a
#: commit limit of ~196 GB with 72 GB of RAM, and the root SSD left alone.
WITH_DISK = [(r"C:\pagefile.sys", 4096), (r"P:\pagefile.sys", 122880)]
#: Without it: 40 GB on C:, a commit limit of ~112 GB, capped because the
#: system disk is a sparse image on the host's root SSD.
WITHOUT_DISK = [(r"C:\pagefile.sys", 40960)]


def _parse(entry):
    parts = entry.split()
    if len(parts) != 3:
        return None
    try:
        return parts[0].lower(), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def wanted_paging_files(current, layout):
    """The PagingFiles entries to write, or None when ``current`` is ``layout``.

    ``layout`` is ``[(path, megabytes), ...]``, each a fixed-size file. The
    current value must be exactly that set: Windows managing the size
    (``?:\\pagefile.sys``, ``0 0``), a different size or an extra file are
    all rewritten, so moving the pagefile off C: also shrinks the C: file.
    """
    have = {p for p in (_parse(e) for e in (current or [])) if p is not None}
    if len(have) == len(current or []) and have == {
        (path.lower(), mb, mb) for path, mb in layout
    }:
        return None
    return [f"{path} {mb} {mb}" for path, mb in layout]


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
            layout = WITH_DISK if os.path.isdir("P:\\") else WITHOUT_DISK
            wanted = wanted_paging_files(current, layout)
            if wanted is None:
                print(f"pagefile ok ({', '.join(wanted_paging_files([], layout))})")
                return 0
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_MULTI_SZ, wanted)
            written, _ = winreg.QueryValueEx(key, VALUE)
    except OSError as exc:
        print(f"pagefile check failed: {exc}")
        return 1
    if wanted_paging_files(written, layout) is not None:
        print("pagefile write did not stick")
        return 1
    print(f"pagefile set to {wanted!r} (was {current!r}); reboot to apply")
    return 3


if __name__ == "__main__":
    sys.exit(main())
