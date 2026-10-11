"""api_runner.bat must cap numpy's BLAS threads before starting the API.

Unset, OpenBLAS sets aside a buffer for every CPU of the VM at `import numpy`,
in each API process: ~780 MB committed per idle process on a 40-vCPU VM. With
many terminals per VM that took 41 GB of commit before a single backtest ran.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "api_runner.bat"


@pytest.mark.parametrize(
    "variable", ["OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"]
)
def test_blas_threads_are_capped_before_the_api_starts(variable):
    text = RUNNER.read_text(encoding="utf-8")
    setting = f'set "{variable}=1"'
    launch = '"%PYDIR%\\python.exe" -m mt5api'

    assert setting in text
    assert text.index(setting) < text.index(launch)
