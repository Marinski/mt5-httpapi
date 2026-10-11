"""Observe loaded OpenBLAS libraries without changing their thread limits."""
from __future__ import annotations

import ctypes
import json
import os

import numpy  # noqa: F401 - load the same native runtime the MT5 SDK uses
import psutil

from mt5api.logger import log

LOG_MARKER = "BLAS_RUNTIME "
THREAD_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
LIBRARY_PREFIXES = ("libopenblas", "libblas", "libscipy_openblas", "openblas")
SYMBOL_PREFIXES = ("", "scipy_")
SYMBOL_SUFFIXES = ("", "64_", "_64")


def snapshot() -> dict:
    libraries = []
    paths = sorted({mapping.path for mapping in psutil.Process().memory_maps()})
    for path in paths:
        name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not name.startswith(LIBRARY_PREFIXES):
            continue
        # Inspect only an already mapped library; never search a DLL load path.
        library = ctypes.CDLL(path, mode=getattr(os, "RTLD_NOLOAD", ctypes.DEFAULT_MODE))
        for prefix in SYMBOL_PREFIXES:
            for suffix in SYMBOL_SUFFIXES:
                getter = getattr(library, f"{prefix}openblas_get_num_threads{suffix}", None)
                if getter is None:
                    continue
                getter.argtypes = []
                getter.restype = ctypes.c_int
                libraries.append({"library": name, "num_threads": getter()})
                break
            else:
                continue
            break
    if not libraries:
        raise RuntimeError("no loaded OpenBLAS thread-count getter found")
    return {
        "pid": os.getpid(),
        "env": {key: os.environ.get(key) for key in THREAD_ENV},
        "libraries": libraries,
    }


def log_snapshot() -> None:
    try:
        report = snapshot()
    except Exception as error:  # noqa: BLE001 - diagnostics must not stop the trading API
        log.warning("BLAS_RUNTIME unavailable (%s)", type(error).__name__)
        return
    log.info("%s%s", LOG_MARKER, json.dumps(report, sort_keys=True))
