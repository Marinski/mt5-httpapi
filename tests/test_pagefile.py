"""scripts/pagefile.py: a fixed pagefile big enough for the testers' commit."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "pagefile", Path(__file__).resolve().parent.parent / "scripts" / "pagefile.py"
)
pagefile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pagefile)

WANTED = [r"C:\pagefile.sys 40960 40960"]


@pytest.mark.parametrize(
    "current",
    [
        [r"?:\pagefile.sys"],  # Windows manages it (the default)
        [r"C:\pagefile.sys 0 0"],  # system-managed size on C:
        [r"C:\pagefile.sys 16384 24576"],  # too small
        [r"C:\pagefile.sys 40960 30000"],  # max below target
        [],  # no pagefile
        None,
    ],
)
def test_anything_short_of_a_fixed_target_file_is_replaced(current):
    assert pagefile.wanted_paging_files(current, 40960) == WANTED


@pytest.mark.parametrize(
    "current",
    [
        [r"C:\pagefile.sys 40960 40960"],
        [r"c:\PAGEFILE.SYS 65536 65536"],
        [r"C:\pagefile.sys 40960 40960", r"D:\pagefile.sys 1024 1024"],
    ],
)
def test_a_large_enough_fixed_file_is_left_alone(current):
    assert pagefile.wanted_paging_files(current, 40960) is None
