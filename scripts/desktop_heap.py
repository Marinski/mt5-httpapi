"""Raise the interactive desktop heap of a Windows VM that runs many terminals.

Every window, console and terminal in the logged-on session draws on that
session's desktop heap, sized by the second field of
``SharedSection=1024,20480,768`` (KB) in the registry value
``HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\SubSystems\\Windows``.
At the 20 MB default a VM running ~20+ terminals plus an API console each
runs out: Windows logs Win32k event 243 ("A desktop heap allocation
failed") and every terminal launched after that exits about a second after
"started for" with code 10053, before reaching the broker or the tester.
On a two-VM install running ~50 terminals each, every burst of 10053 aborts
over a week followed a 243 event on the same VM.

Opt-in: start.bat runs this only when config.yaml sets ``desktop_heap_kb``.

    desktop_heap.py --kb 65536   raise the interactive heap to 65536 KB
    desktop_heap.py --restore    write back the value saved before the first raise

Before its first write it saves the whole original value and its registry
type to BACKUP_PATH, on the VM's own disk, and never overwrites that copy,
so ``--restore`` always returns to what Windows had before this script.
A value it cannot parse is refused rather than rewritten.

Exit codes:
    0  nothing to change (already large enough, already restored, or not asked)
    3  the value changed; it takes effect on the next boot, so the caller reboots
    1  refused or failed: bad argument, malformed value, registry or backup error
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\SubSystems"
VALUE = "Windows"
#: The value docs/operations.md suggests for many terminals per VM: 64 MB.
TARGET_KB = 65536
#: Windows' own default. A target at or below it would change nothing.
DEFAULT_KB = 20480
#: Upper bound on what --kb accepts: the session's desktop heaps share its view
#: space, so an absurd value is far more likely a typo than an intent.
MAX_KB = 131072
#: VM-local (not on the Shared folder, which every VM sees), so each VM keeps
#: its own original.
BACKUP_PATH = os.path.join(
    os.environ.get("ProgramData") or r"C:\ProgramData",
    "mt5-httpapi",
    "desktop-heap-original.json",
)

_SHARED_SECTION_KEY = re.compile(r"SharedSection=", re.IGNORECASE)
# The fields after "SharedSection=": two or three unsigned integers separated by
# commas, with optional whitespace around each comma (Microsoft KB 947246 shows
# "1024, 20480,768"), ending at whitespace or the end of the value. Anything
# else - "20480oops", "-20480", a trailing comma - does not match.
_FIELDS = re.compile(r"\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?(?!\s*,)(?=\s|$)")


def _interactive_field(windows_value):
    """The match whose group 2 is the interactive heap field.

    Raises ValueError when the value has no SharedSection or its fields are
    malformed, so a value this script does not understand is never written.
    """
    key = _SHARED_SECTION_KEY.search(windows_value)
    if key is None:
        raise ValueError("no SharedSection= in the Windows subsystem value")
    fields = _FIELDS.match(windows_value, key.end())
    if fields is None:
        raise ValueError(
            "SharedSection fields are not two or three unsigned integers: "
            + windows_value[key.start() :].split(" ", 1)[0]
        )
    return fields


def raised_shared_section(windows_value, target_kb=TARGET_KB):
    """``windows_value`` with the interactive heap raised to ``target_kb``.

    None when it is already at least that large. Every other byte of the value
    is kept as it was. Raises ValueError for a value it cannot parse.
    """
    fields = _interactive_field(windows_value)
    if int(fields.group(2)) >= target_kb:
        return None
    return windows_value[: fields.start(2)] + str(target_kb) + windows_value[fields.end(2) :]


def _target_kb(raw):
    try:
        kb = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a whole number of KB: {raw!r}") from None
    if not DEFAULT_KB < kb <= MAX_KB:
        raise argparse.ArgumentTypeError(
            f"{kb} KB is outside {DEFAULT_KB + 1}..{MAX_KB} "
            f"(the Windows default is {DEFAULT_KB} KB)"
        )
    return kb


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--kb", type=_target_kb, help="raise the interactive heap to this many KB")
    action.add_argument(
        "--restore", action="store_true", help="write back the value saved before the first raise"
    )
    return parser.parse_args(argv)


def _save_original(value, value_type):
    """Keep the pre-change value. The first saved copy is never overwritten."""
    if os.path.exists(BACKUP_PATH):
        _load_original()
        return
    os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
    tmp = BACKUP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"key": KEY, "value_name": VALUE, "value": value, "type": value_type}, handle)
    os.replace(tmp, BACKUP_PATH)


def _load_original():
    with open(BACKUP_PATH, encoding="utf-8") as handle:
        saved = json.load(handle)
    if (
        not isinstance(saved, dict)
        or not isinstance(saved.get("value"), str)
        or type(saved.get("type")) is not int
    ):
        raise TypeError(f"{BACKUP_PATH} does not hold a saved value and type")
    return saved["value"], saved["type"]


def _raise(winreg, target_kb):
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            KEY,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            value, value_type = winreg.QueryValueEx(key, VALUE)
            new_value = raised_shared_section(value, target_kb)
            if new_value is None:
                print(f"desktop heap ok (>= {target_kb} KB)")
                return 0
            # No write without a way back.
            _save_original(value, value_type)
            winreg.SetValueEx(key, VALUE, 0, value_type, new_value)
            written, _ = winreg.QueryValueEx(key, VALUE)
    except (OSError, ValueError, TypeError) as exc:
        print(f"desktop heap check failed: {exc}")
        return 1
    try:
        still_low = raised_shared_section(written, target_kb) is not None
    except ValueError:
        still_low = True
    if still_low:
        print("desktop heap write did not stick")
        return 1
    print(f"desktop heap raised to {target_kb} KB (original saved to {BACKUP_PATH}); reboot to apply")
    return 3


def _restore(winreg):
    try:
        original, original_type = _load_original()
    except FileNotFoundError:
        print(f"desktop heap restore: no saved original at {BACKUP_PATH}; nothing to restore")
        return 1
    except (OSError, ValueError, TypeError) as exc:
        print(f"desktop heap restore failed: {exc}")
        return 1
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            KEY,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            current, _ = winreg.QueryValueEx(key, VALUE)
            if current == original:
                print("desktop heap already at the saved original")
                return 0
            winreg.SetValueEx(key, VALUE, 0, original_type, original)
            written, _ = winreg.QueryValueEx(key, VALUE)
    except OSError as exc:
        print(f"desktop heap restore failed: {exc}")
        return 1
    if written != original:
        print("desktop heap restore did not stick")
        return 1
    print("desktop heap restored to the saved original; reboot to apply")
    return 3


def main(argv=None):
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        # argparse already printed why.
        return 1
    if args.kb is None and not args.restore:
        print("desktop heap not configured (desktop_heap_kb unset); left as Windows set it")
        return 0
    import winreg

    if args.restore:
        return _restore(winreg)
    return _raise(winreg, args.kb)


if __name__ == "__main__":
    sys.exit(main())
