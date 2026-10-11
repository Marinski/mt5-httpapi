"""Native runtime inspection and live log evidence must not certify stale workers."""
from __future__ import annotations

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mt5api import blas_runtime
from tests.live.test_health_regressions import latest_blas_report


@pytest.mark.parametrize("symbol", ["openblas_get_num_threads", "openblas_get_num_threads64_",
                                    "scipy_openblas_get_num_threads", "openblas_get_num_threads_64"])
def test_reports_the_native_getter_not_the_environment(monkeypatch, symbol):
    getter = Mock(return_value=7)
    library = SimpleNamespace(**{symbol: getter})
    process = SimpleNamespace(memory_maps=lambda: [
        SimpleNamespace(path="C:\\numpy.libs\\libopenblas.dll"),
        SimpleNamespace(path="C:\\numpy.libs\\libopenblas.dll"),
        SimpleNamespace(path="C:\\other.dll"),
    ])
    monkeypatch.setattr(blas_runtime.psutil, "Process", lambda: process)
    loader = Mock(return_value=library)
    monkeypatch.setattr(blas_runtime.ctypes, "CDLL", loader)
    for variable in blas_runtime.THREAD_ENV:
        monkeypatch.setenv(variable, "1")

    report = blas_runtime.snapshot()

    assert report["env"] == {variable: "1" for variable in blas_runtime.THREAD_ENV}
    assert report["libraries"] == [{"library": "libopenblas.dll", "num_threads": 7}]
    assert getter.restype is ctypes.c_int
    assert getter.argtypes == []
    loader.assert_called_once()


def test_missing_native_getter_is_a_failure(monkeypatch):
    monkeypatch.setattr(blas_runtime.psutil, "Process", lambda: SimpleNamespace(
        memory_maps=lambda: [SimpleNamespace(path="/other.so")],
    ))
    with pytest.raises(RuntimeError, match="no loaded OpenBLAS"):
        blas_runtime.snapshot()


def test_observation_failure_is_logged_without_stopping_the_api(monkeypatch):
    monkeypatch.setattr(blas_runtime, "snapshot", Mock(side_effect=OSError("private path")))
    logger = Mock()
    monkeypatch.setattr(blas_runtime, "log", logger)
    blas_runtime.log_snapshot()
    logger.warning.assert_called_once_with("BLAS_RUNTIME unavailable (%s)", "OSError")
    logger.info.assert_not_called()


def test_real_numpy_library_exposes_its_thread_count():
    report = blas_runtime.snapshot()
    assert report["libraries"]
    assert all(entry["num_threads"] >= 1 for entry in report["libraries"])


def test_successful_observation_is_logged(monkeypatch):
    report = {"pid": 20, "libraries": [{"num_threads": 1}]}
    monkeypatch.setattr(blas_runtime, "snapshot", lambda: report)
    logger = Mock()
    monkeypatch.setattr(blas_runtime, "log", logger)
    blas_runtime.log_snapshot()
    logger.info.assert_called_once_with(
        "%s%s", "BLAS_RUNTIME ", '{"libraries": [{"num_threads": 1}], "pid": 20}',
    )


@pytest.mark.parametrize("tail", ["=== PROCESS STARTED ===\n", "BLAS_RUNTIME unavailable (OSError)\n"])
def test_prior_process_or_failed_observation_cannot_pass_live_check(tmp_path, tail):
    path = tmp_path / "api.log"
    path.write_text('BLAS_RUNTIME {"pid": 10}\n' + tail, encoding="utf-8")
    with pytest.raises(AssertionError, match="latest API process"):
        latest_blas_report(path)


def test_latest_process_record_is_selected(tmp_path):
    path = tmp_path / "api.log"
    path.write_text(
        'BLAS_RUNTIME {"pid": 10}\n=== PROCESS STARTED ===\nBLAS_RUNTIME {"pid": 20}\n',
        encoding="utf-8",
    )
    assert latest_blas_report(path) == {"pid": 20}
