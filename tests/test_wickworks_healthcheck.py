"""Tests for the wickworks sidecar self-heal healthcheck.

The wickworks TA sidecar shares the mt5 VM container's netns via compose
``network_mode: service:mt5``. When the mt5 container is recreated, Docker
leaves wickworks in the old, now-orphaned netns: its loopback /health still
answers (so the image's built-in check stays green) while the VM can no
longer reach it. The healthcheck must (a) stay green while the namespaces are
shared, (b) go red AND kill wickworks when the dockurr gateway becomes
unreachable, so compose's restart policy recreates it into the current netns.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wickworks-healthcheck.py"


def _load():
    spec = importlib.util.spec_from_file_location("wickworks_hc_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_sock(opens):
    """Returns a connect_ex-spoofing socket whose result depends on `opens`."""
    class Sock:
        def __init__(self, *args, **kwargs):
            pass

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 0 if opens else 111

        def close(self):
            pass

    return Sock


@pytest.fixture
def hc():
    return _load()


def test_healthy_when_self_and_gateway_reachable(hc):
    with patch("urllib.request.urlopen", return_value=_Resp(200)), \
         patch("socket.socket", _fake_sock(True)), \
         patch("os.kill") as kill:
        assert hc.main() == 0
        kill.assert_not_called()


def test_unhealthy_when_self_down_no_restart(hc):
    with patch("urllib.request.urlopen", side_effect=OSError("refused")), \
         patch("os.kill") as kill:
        assert hc.main() == 1
        kill.assert_not_called()


def test_orphan_kills_main_process(hc):
    """Gateway unreachable + self up => orphaned => kill uvicorn, exit 1."""
    with patch("urllib.request.urlopen", return_value=_Resp(200)), \
         patch("socket.socket", _fake_sock(False)), \
         patch("os.listdir", return_value=["1", "7", "self"]), \
         patch("builtins.open", create=True) as mock_open:
        # os.listdir drives the /proc scan in _kill_main_process.
        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/7/cmdline"):
                fh = type("FH", (), {"__enter__": lambda s: s, "__exit__": lambda *a: False})()
                fh.read = lambda: b"/opt/venv/bin/python /opt/venv/bin/uvicorn wickworks.server:app"
                return fh
            raise FileNotFoundError(path)

        mock_open.side_effect = fake_open
        with patch("os.kill") as kill:
            assert hc.main() == 1
            # One kill call: the uvicorn main process (pid 7).
            assert kill.call_count == 1
            assert kill.call_args[0][0] == 7


def test_orphan_skips_uvicorn_workers(hc):
    """Only the uvicorn MAIN process is killed, not a --multiprocessing-fork
    worker, so the worker-set shutdown path is untouched."""
    with patch("urllib.request.urlopen", return_value=_Resp(200)), \
         patch("socket.socket", _fake_sock(False)), \
         patch("os.listdir", return_value=["1", "9"]), \
         patch("builtins.open", create=True) as mock_open:
        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/9/cmdline"):
                fh = type("FH", (), {"__enter__": lambda s: s, "__exit__": lambda *a: False})()
                fh.read = lambda: b"python -B -c from multiprocessing.spawn import spawn_main ... --multiprocessing-fork"
                return fh
            raise FileNotFoundError(path)

        mock_open.side_effect = fake_open
        with patch("os.kill") as kill:
            assert hc.main() == 1
            # Worker is skipped; fallback SIGTERM to PID 1 happens instead.
            assert kill.call_count == 1
            assert kill.call_args[0][0] == 1
            assert kill.call_args[0][1] == 15
