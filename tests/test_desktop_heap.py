"""scripts/desktop_heap.py: raise the VM's interactive desktop heap.

At the Windows default (20 MB) a VM running 20+ terminals runs out, Windows
logs Win32k event 243, and each terminal launched after that aborts at
startup with exit code 10053.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "desktop_heap", Path(__file__).resolve().parent.parent / "scripts" / "desktop_heap.py"
)
desktop_heap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(desktop_heap)

#: Arguments start.bat passes when config.yaml sets desktop_heap_kb.
ENABLE = ["--kb", str(desktop_heap.TARGET_KB)]

#: The value as Windows 11 22H2 ships it.
DEFAULT = (
    r"%SystemRoot%\system32\csrss.exe ObjectDirectory=\Windows "
    r"SharedSection=1024,20480,768 Windows=On SubSystemType=Windows "
    r"ServerDll=basesrv,1 ServerDll=winsrv:UserServerDllInitialization,3 "
    r"ServerDll=sxssrv,4 ProfileControl=Off MaxRequestThreads=16"
)


def test_raises_only_the_interactive_heap():
    raised = desktop_heap.raised_shared_section(DEFAULT, 65536)

    assert "SharedSection=1024,65536,768" in raised
    assert raised.replace("65536", "20480") == DEFAULT


def test_leaves_a_large_enough_heap_alone():
    already = DEFAULT.replace("1024,20480,768", "1024,81920,768")

    assert desktop_heap.raised_shared_section(already, 65536) is None
    assert desktop_heap.raised_shared_section(DEFAULT.replace("20480", "65536"), 65536) is None


def test_two_field_shared_section_is_raised_too():
    two = DEFAULT.replace("1024,20480,768", "1024,20480")

    assert "SharedSection=1024,65536 " in desktop_heap.raised_shared_section(two, 65536)


def test_a_value_without_shared_section_is_refused():
    with pytest.raises(ValueError):
        desktop_heap.raised_shared_section("csrss.exe Windows=On", 65536)


@pytest.mark.parametrize(
    "section,current_kb",
    [
        ("SharedSection=1024, 20480,768", 20480),
        ("SharedSection=1024, 12288,512", 12288),
    ],
)
def test_whitespace_in_shared_section_keeps_every_other_byte(
    section: str,
    current_kb: int,
) -> None:
    # Microsoft KB 947246 includes whitespace after the first comma.
    value = DEFAULT.replace("SharedSection=1024,20480,768", section)

    raised = desktop_heap.raised_shared_section(value)

    assert raised == value.replace(str(current_kb), str(desktop_heap.TARGET_KB))


def test_shared_section_is_case_insensitive() -> None:
    value = DEFAULT.replace("SharedSection", "sHaReDsEcTiOn")

    assert desktop_heap.raised_shared_section(value) == value.replace(
        "20480", str(desktop_heap.TARGET_KB)
    )


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_custom_target_changes_only_values_below_it(delta: int) -> None:
    target = 24576
    value = DEFAULT.replace("20480", str(target + delta))

    raised = desktop_heap.raised_shared_section(value, target)

    if delta < 0:
        assert raised == value.replace(str(target + delta), str(target))
        return
    assert raised is None


@pytest.fixture(autouse=True)
def backup_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ProgramData" / "mt5-httpapi" / "desktop-heap-original.json"
    monkeypatch.setattr(desktop_heap, "BACKUP_PATH", str(path))
    return path


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    key = MagicMock(name="registry_key")
    context = MagicMock(name="registry_context")
    context.__enter__.return_value = key
    context.__exit__.return_value = False
    module = SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        KEY_READ=0x20019,
        KEY_SET_VALUE=0x0002,
        REG_EXPAND_SZ=2,
        OpenKey=Mock(return_value=context),
        QueryValueEx=Mock(return_value=(DEFAULT, 2)),
        SetValueEx=Mock(),
        key=key,
        context=context,
    )
    monkeypatch.setitem(sys.modules, "winreg", module)
    return module


def test_main_writes_verifies_and_closes_the_registry_key(
    registry: SimpleNamespace,
) -> None:
    expected = DEFAULT.replace("20480", str(desktop_heap.TARGET_KB))
    registry.QueryValueEx.side_effect = [
        (DEFAULT, registry.REG_EXPAND_SZ),
        (expected, registry.REG_EXPAND_SZ),
    ]

    assert desktop_heap.main(ENABLE) == 3

    registry.OpenKey.assert_called_once_with(
        registry.HKEY_LOCAL_MACHINE,
        desktop_heap.KEY,
        0,
        registry.KEY_READ | registry.KEY_SET_VALUE,
    )
    registry.SetValueEx.assert_called_once_with(
        registry.key,
        desktop_heap.VALUE,
        0,
        registry.REG_EXPAND_SZ,
        expected,
    )
    assert registry.QueryValueEx.call_count == 2
    for call in registry.QueryValueEx.call_args_list:
        assert call.args == (registry.key, desktop_heap.VALUE)
    registry.context.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("current", [65536, 81920])
def test_main_does_not_write_or_request_reboot_when_already_large_enough(
    registry: SimpleNamespace,
    current: int,
) -> None:
    value = DEFAULT.replace("20480", str(current))
    registry.QueryValueEx.return_value = (value, registry.REG_EXPAND_SZ)

    assert desktop_heap.main(ENABLE) == 0

    registry.SetValueEx.assert_not_called()
    registry.QueryValueEx.assert_called_once_with(registry.key, desktop_heap.VALUE)
    registry.context.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "csrss.exe Windows=On",
        "SharedSection=1024,",
        "SharedSection=1024,unknown,768",
        "SharedSection=1024,-20480,768",
        "SharedSection=1024,20480oops,768",
        "SharedSection=1024,20480 ,768oops Windows=On",
        "SharedSection=1024,20480 , Windows=On",
        "SharedSection=1024,20480 ,768,999 Windows=On",
    ],
)
def test_main_refuses_malformed_heap_values_without_writing(
    registry: SimpleNamespace,
    value: str,
) -> None:
    registry.QueryValueEx.return_value = (value, registry.REG_EXPAND_SZ)

    assert desktop_heap.main(ENABLE) == 1

    registry.SetValueEx.assert_not_called()
    registry.context.__exit__.assert_called_once()


@pytest.mark.parametrize("stage", ["open", "read", "write", "verify"])
def test_registry_failures_never_report_success_or_request_reboot(
    registry: SimpleNamespace,
    stage: str,
) -> None:
    failure = OSError("registry operation denied")
    if stage == "open":
        registry.OpenKey.side_effect = failure
    elif stage == "read":
        registry.QueryValueEx.side_effect = failure
    elif stage == "write":
        registry.SetValueEx.side_effect = failure
    else:
        registry.QueryValueEx.side_effect = [
            (DEFAULT, registry.REG_EXPAND_SZ),
            failure,
        ]

    assert desktop_heap.main(ENABLE) == 1

    if stage in ("open", "read"):
        registry.SetValueEx.assert_not_called()
    else:
        registry.SetValueEx.assert_called_once()
    if stage == "open":
        registry.context.__enter__.assert_not_called()
        return
    registry.context.__exit__.assert_called_once()


def test_readback_below_target_reports_failure_instead_of_reboot(
    registry: SimpleNamespace,
) -> None:
    registry.QueryValueEx.return_value = (DEFAULT, registry.REG_EXPAND_SZ)

    assert desktop_heap.main(ENABLE) == 1

    registry.SetValueEx.assert_called_once()
    assert registry.QueryValueEx.call_count == 2


def test_second_invocation_does_not_write_again(registry: SimpleNamespace) -> None:
    expected = DEFAULT.replace("20480", str(desktop_heap.TARGET_KB))
    registry.QueryValueEx.side_effect = [
        (DEFAULT, registry.REG_EXPAND_SZ),
        (expected, registry.REG_EXPAND_SZ),
        (expected, registry.REG_EXPAND_SZ),
    ]

    assert desktop_heap.main(ENABLE) == 3
    assert desktop_heap.main(ENABLE) == 0

    registry.SetValueEx.assert_called_once()
    assert registry.QueryValueEx.call_count == 3
    assert registry.context.__exit__.call_count == 2


# --- opt-in, argument validation, backup and restore ---------------------------


def test_without_arguments_nothing_is_read_or_written(registry: SimpleNamespace) -> None:
    """desktop_heap_kb unset: start.bat passes nothing, and Windows keeps its value."""
    assert desktop_heap.main([]) == 0

    registry.OpenKey.assert_not_called()
    registry.SetValueEx.assert_not_called()


@pytest.mark.parametrize("raw", ["20480", "1024", "0", "-1", "131073", "64MB", "65536.5", ""])
def test_out_of_range_or_malformed_targets_are_refused(
    registry: SimpleNamespace, raw: str
) -> None:
    assert desktop_heap.main(["--kb", raw]) == 1

    registry.OpenKey.assert_not_called()
    registry.SetValueEx.assert_not_called()


def test_the_original_is_saved_before_the_first_write(
    registry: SimpleNamespace, backup_path: Path
) -> None:
    expected = DEFAULT.replace("20480", str(desktop_heap.TARGET_KB))
    saved_at_write: list[bool] = []
    registry.SetValueEx.side_effect = lambda *a: saved_at_write.append(backup_path.exists())
    registry.QueryValueEx.side_effect = [
        (DEFAULT, registry.REG_EXPAND_SZ),
        (expected, registry.REG_EXPAND_SZ),
    ]

    assert desktop_heap.main(ENABLE) == 3

    assert saved_at_write == [True]
    saved = json.loads(backup_path.read_text(encoding="utf-8"))
    assert saved["value"] == DEFAULT
    assert saved["type"] == registry.REG_EXPAND_SZ


def test_a_later_raise_never_overwrites_the_saved_original(
    registry: SimpleNamespace, backup_path: Path
) -> None:
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text(json.dumps({"value": "ORIGINAL", "type": 2}), encoding="utf-8")
    expected = DEFAULT.replace("20480", "81920")
    registry.QueryValueEx.side_effect = [
        (DEFAULT, registry.REG_EXPAND_SZ),
        (expected, registry.REG_EXPAND_SZ),
    ]

    assert desktop_heap.main(["--kb", "81920"]) == 3

    assert json.loads(backup_path.read_text(encoding="utf-8"))["value"] == "ORIGINAL"


def test_no_write_when_the_original_cannot_be_saved(
    registry: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object) -> None:
        raise OSError("ProgramData is read-only")

    monkeypatch.setattr(desktop_heap, "_save_original", refuse)

    assert desktop_heap.main(ENABLE) == 1

    registry.SetValueEx.assert_not_called()


@pytest.mark.parametrize("contents", ["{broken-json", "{}", "[]", "null"])
def test_an_unusable_existing_backup_prevents_a_registry_write(
    registry: SimpleNamespace,
    backup_path: Path,
    contents: str,
) -> None:
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text(contents, encoding="utf-8")
    expected = DEFAULT.replace("20480", str(desktop_heap.TARGET_KB))
    registry.QueryValueEx.side_effect = [
        (DEFAULT, registry.REG_EXPAND_SZ),
        (expected, registry.REG_EXPAND_SZ),
    ]

    result = desktop_heap.main(ENABLE)

    registry.SetValueEx.assert_not_called()
    assert result == 1
    assert backup_path.read_text(encoding="utf-8") == contents


def test_restore_writes_back_the_saved_value_and_type(
    registry: SimpleNamespace, backup_path: Path
) -> None:
    raised = DEFAULT.replace("20480", str(desktop_heap.TARGET_KB))
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text(
        json.dumps({"value": DEFAULT, "type": registry.REG_EXPAND_SZ}), encoding="utf-8"
    )
    registry.QueryValueEx.side_effect = [
        (raised, registry.REG_EXPAND_SZ),
        (DEFAULT, registry.REG_EXPAND_SZ),
    ]

    assert desktop_heap.main(["--restore"]) == 3

    registry.SetValueEx.assert_called_once_with(
        registry.key, desktop_heap.VALUE, 0, registry.REG_EXPAND_SZ, DEFAULT
    )


def test_restore_is_a_no_op_once_back_at_the_original(
    registry: SimpleNamespace, backup_path: Path
) -> None:
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text(json.dumps({"value": DEFAULT, "type": 2}), encoding="utf-8")
    registry.QueryValueEx.return_value = (DEFAULT, registry.REG_EXPAND_SZ)

    assert desktop_heap.main(["--restore"]) == 0

    registry.SetValueEx.assert_not_called()


def test_restore_without_a_saved_original_writes_nothing(registry: SimpleNamespace) -> None:
    assert desktop_heap.main(["--restore"]) == 1

    registry.OpenKey.assert_not_called()
    registry.SetValueEx.assert_not_called()


def test_kb_and_restore_are_mutually_exclusive(registry: SimpleNamespace) -> None:
    assert desktop_heap.main(["--kb", "65536", "--restore"]) == 1

    registry.OpenKey.assert_not_called()
