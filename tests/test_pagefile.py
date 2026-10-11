"""scripts/pagefile.py: fixed pagefiles big enough for the testers' commit."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "pagefile", Path(__file__).resolve().parent.parent / "scripts" / "pagefile.py"
)
pagefile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pagefile)


def test_the_layouts():
    assert pagefile.wanted_paging_files([], pagefile.WITH_DISK) == [
        r"C:\pagefile.sys 4096 4096",
        r"P:\pagefile.sys 122880 122880",
    ]
    assert pagefile.wanted_paging_files([], pagefile.WITHOUT_DISK) == [
        r"C:\pagefile.sys 40960 40960"
    ]


@pytest.mark.parametrize(
    "current",
    [
        [r"?:\pagefile.sys"],  # Windows manages it (the default)
        [r"C:\pagefile.sys 0 0"],  # system-managed size on C:
        [r"C:\pagefile.sys 40960 40960"],  # the no-disk layout: C: must shrink
        [r"P:\pagefile.sys 122880 122880"],  # no C: file
        [r"C:\pagefile.sys 4096 4096", r"P:\pagefile.sys 65536 65536"],
        [r"C:\pagefile.sys 4096 4096", r"P:\pagefile.sys 122880 122880", "junk"],
        [],
        None,
    ],
)
def test_anything_but_the_exact_disk_layout_is_rewritten(current):
    assert pagefile.wanted_paging_files(current, pagefile.WITH_DISK) is not None


@pytest.mark.parametrize(
    "current, layout",
    [
        ([r"C:\pagefile.sys 4096 4096", r"P:\pagefile.sys 122880 122880"], "WITH_DISK"),
        ([r"p:\PAGEFILE.SYS 122880 122880", r"c:\pagefile.sys 4096 4096"], "WITH_DISK"),
        ([r"C:\pagefile.sys 40960 40960"], "WITHOUT_DISK"),
    ],
)
def test_the_exact_layout_is_left_alone(current, layout):
    assert pagefile.wanted_paging_files(current, getattr(pagefile, layout)) is None
