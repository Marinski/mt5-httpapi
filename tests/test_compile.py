"""Contract tests for POST /compile.

The endpoint takes caller-supplied MQL5 text, hands it to MetaEditor, and
returns a .ex5. Two classes of thing are worth pinning here:

  * The contract itself. Clients are written against it, and its two
    hard rules — `ok: true` implies a non-empty binary, and `log` is always a
    string — are the ones that break the caller silently if they regress.
  * The MetaEditor quirks the implementation exists to absorb: a UTF-16LE log,
    an exit code that goes non-zero on warnings, and a compiler that reports
    success in the log while producing no file.

MetaEditor itself is a Windows binary, so subprocess.run is patched. What is
NOT patched is the log decoding or the count parsing — those run against real
UTF-16LE bytes, because that is where the bugs live.
"""

import base64
import builtins
import json
import multiprocessing
import os
import pathlib
import threading
import time

import pytest

from mt5api.handlers import compile as compile_handler
from tests import compile_lock_worker


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utf16_log(text):
    """Bytes exactly as MetaEditor writes them: UTF-16LE with a BOM, CRLF."""
    return text.replace("\n", "\r\n").encode("utf-16")


SUCCESS_LOG = (
    "MetaEditor 5 build 4885 started\n"
    "ea.mq5 : information: compiling 'ea.mq5'\n"
    "Result: 0 errors, 0 warnings, 121 msec elapsed\n"
)

WARNING_LOG = (
    "ea.mq5(14,7) : warning 43: possible loss of data due to type conversion\n"
    "Result: 0 errors, 2 warnings, 138 msec elapsed\n"
)

ERROR_LOG = (
    "ea.mq5(12,5) : error 160: expression of 'void' type is illegal\n"
    "ea.mq5(13,1) : error 145: '}' - unexpected end of program\n"
    "ea.mq5(13,1) : error 100: ';' - semicolon expected\n"
    "Result: 3 errors, 0 warnings, 96 msec elapsed\n"
)

MISSING_INCLUDE_LOG = (
    "ea.mq5(3,11) : error 133: cannot open include file "
    "'SomeLibrary.mqh'\n"
    "Result: 1 errors, 0 warnings, 44 msec elapsed\n"
)


class FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode


@pytest.fixture
def compile_env(monkeypatch, tmp_path):
    """Point the handler at a temp workspace and a MetaEditor that 'exists'."""
    fake_editor = tmp_path / "MetaEditor64.exe"
    fake_editor.write_bytes(b"MZ")
    work = tmp_path / "work"
    monkeypatch.setattr(compile_handler, "COMPILE_METAEDITOR", str(fake_editor))
    monkeypatch.setattr(compile_handler, "COMPILE_WORK_DIR", str(work))
    monkeypatch.setattr(compile_handler, "COMPILE_INCLUDE_DIR", str(tmp_path / "MQL5"))
    monkeypatch.setattr(compile_handler, "COMPILE_TIMEOUT_SECONDS", 30)
    return {"work": work, "editor": str(fake_editor)}


def _fake_metaeditor(log_bytes, ex5_bytes=None, returncode=0, record=None):
    """Stand in for MetaEditor: writes the log it was given, and an .ex5 when
    the compile is meant to have produced one."""
    def _run(cmd, **kwargs):
        if record is not None:
            record.append(cmd)
        log_path = next(a.split(":", 1)[1] for a in cmd if a.startswith("/log:"))
        src_path = next(a.split(":", 1)[1] for a in cmd if a.startswith("/compile:"))
        with open(log_path, "wb") as fh:
            fh.write(log_bytes)
        if ex5_bytes is not None:
            with open(os.path.splitext(src_path)[0] + ".ex5", "wb") as fh:
                fh.write(ex5_bytes)
        return FakeCompleted(returncode)
    return _run


API_TOKEN = "full-api-token"
COMPILE_TOKEN = "compile-only-token"


@pytest.fixture
def client(monkeypatch):
    """Flask client with BOTH tokens configured, so the split is exercised."""
    from mt5api import server

    monkeypatch.setattr(server, "API_TOKEN", API_TOKEN)
    monkeypatch.setattr(server, "COMPILE_API_TOKEN", COMPILE_TOKEN)
    server.app.config["TESTING"] = True
    return server.app.test_client()


def _post(client, body, token=COMPILE_TOKEN):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/compile", data=json.dumps(body), headers=headers)


# ── Log decoding and count parsing (no subprocess involved) ──────────────────

def test_metaeditor_log_is_decoded_as_utf16():
    # Decoding UTF-16LE as UTF-8 yields NUL-riddled mojibake and every count
    # regex silently stops matching, so this is the load-bearing decode.
    text = compile_handler._read_metaeditor_log_bytes(_utf16_log(SUCCESS_LOG))
    assert "Result: 0 errors, 0 warnings" in text
    assert "\x00" not in text


def test_counts_come_from_the_summary_line():
    assert compile_handler._parse_counts(ERROR_LOG) == (3, 0)
    assert compile_handler._parse_counts(WARNING_LOG) == (0, 2)
    assert compile_handler._parse_counts(SUCCESS_LOG) == (0, 0)


def test_counts_fall_back_to_counting_diagnostics_without_a_summary():
    no_summary = (
        "ea.mq5(12,5) : error 160: bad\n"
        "ea.mq5(13,1) : error 145: worse\n"
        "ea.mq5(14,2) : warning 43: meh\n"
    )
    assert compile_handler._parse_counts(no_summary) == (2, 1)


def test_log_tail_is_capped_at_8kb_and_never_none():
    assert compile_handler._tail(None) == ""
    assert compile_handler._tail("") == ""
    big = "x" * 20000
    assert len(compile_handler._tail(big).encode("utf-8")) <= 8192
    # Multi-byte content must not be cut mid-character.
    assert compile_handler._tail("é" * 20000).encode("utf-8")


# ── Filename handling: source text only, never a caller-supplied path ────────

@pytest.mark.parametrize("supplied,expected", [
    ("ea.mq5", "ea"),
    ("MyEA.mq5", "MyEA"),
    ("../../terminal64", "terminal64"),
    ("..\\..\\Windows\\system32\\evil.mq5", "evil"),
    ("C:\\Windows\\x.mq5", "x"),
    ("/etc/passwd", "passwd"),
    ("", "ea"),
    (None, "ea"),
    ("...", "ea"),
    ("a b;c&d.mq5", "a_b_c_d"),
])
def test_filename_is_reduced_to_a_harmless_stem(supplied, expected):
    assert compile_handler._safe_stem(supplied) == expected


# ── The contract ─────────────────────────────────────────────────────────────

def test_success_returns_the_binary_and_a_zero_warning_count(client, compile_env, monkeypatch):
    ex5 = b"\x00ex5-binary-content" * 40
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), ex5),
    )
    resp = _post(client, {"source": "void OnTick(){}", "filename": "ea.mq5"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert base64.b64decode(body["ex5_base64"]) == ex5
    assert body["warnings"] == 0
    assert isinstance(body["log"], str)


def test_warnings_do_not_fail_the_compile(client, compile_env, monkeypatch):
    # MetaEditor exits NON-ZERO on warnings. Trusting the exit code here would
    # turn every warning into a failed compile.
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(WARNING_LOG), b"ex5", returncode=1),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert resp.get_json()["warnings"] == 2


def test_compile_errors_return_422_with_the_compiler_message(client, compile_env, monkeypatch):
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(ERROR_LOG), None, returncode=1),
    )
    resp = _post(client, {"source": "garbage"})
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["ok"] is False
    assert body["errors"] == 3
    assert "expression of 'void' type is illegal" in body["log"]
    assert "ex5_base64" not in body


def test_missing_include_is_a_compile_error_not_a_500(client, compile_env, monkeypatch):
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(MISSING_INCLUDE_LOG), None, returncode=1),
    )
    resp = _post(client, {"source": "#include <SomeLibrary.mqh>"})
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["errors"] == 1
    assert "cannot open include file" in body["log"]


def test_clean_log_without_a_binary_is_never_reported_as_success(client, compile_env, monkeypatch):
    # The one outcome the client cannot recover from: ok:true with no .ex5.
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), None, returncode=0),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["ok"] is False
    assert body["errors"] >= 1
    assert "produced no .ex5" in body["log"]


def test_empty_binary_is_not_success(client, compile_env, monkeypatch):
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b""),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 422


def test_timeout_returns_504_with_a_string_log(client, compile_env, monkeypatch):
    def _timeout(cmd, **kwargs):
        raise compile_handler.subprocess.TimeoutExpired(cmd, 30)
    monkeypatch.setattr(compile_handler.subprocess, "run", _timeout)
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 504
    body = resp.get_json()
    assert body["ok"] is False
    assert body["log"] == "compile timeout after 30s"


def test_missing_source_is_rejected_as_json(client, compile_env):
    for body in [{}, {"source": ""}, {"source": "   "}, {"source": 5}]:
        resp = _post(client, body)
        assert resp.status_code == 400
        assert isinstance(resp.get_json()["log"], str)


def test_missing_content_length_is_refused_with_411(client, compile_env):
    """request.content_length is None for a chunked request (no
    Content-Length declared) -- confirmed live against a real waitress
    server sending genuinely chunked HTTP (deep-qa audit). Without this
    check that skips the pre-parse 413 guard entirely and lets get_json()
    buffer an unbounded body before the decoded-source check downstream ever
    runs.

    Flask's test CLIENT recomputes Content-Length even when the environ key
    is deleted before client.open(), so this drives the view directly inside
    a request context with the header genuinely absent -- matching what a
    real chunked request looks like server-side -- rather than through
    client.post().
    """
    from flask import request

    from mt5api import server

    with server.app.test_request_context(
        "/compile", method="POST", data=b'{"source": "x"}',
        content_type="application/json",
    ):
        del request.environ["CONTENT_LENGTH"]
        assert request.content_length is None  # sanity: this IS the scenario

        resp_body, status = compile_handler.compile_source()

    assert status == 411
    assert "Content-Length" in resp_body.get_json()["log"]


def test_handler_never_raises_and_always_answers_json(client, compile_env, monkeypatch):
    def _boom(cmd, **kwargs):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(compile_handler.subprocess, "run", _boom)
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 500
    assert resp.is_json
    assert isinstance(resp.get_json()["log"], str)


def test_an_unexpected_error_does_not_leak_its_class_message_or_paths(
    client, compile_env, monkeypatch
):
    """The 500 body must be generic. The exception's class and message used to
    be echoed to the caller, which turned any provocable compiler or
    filesystem error into a readout of internal paths - the message of an
    OSError IS a path. Detail belongs in the server log only."""
    secret = "C:\\internal\\deploy\\path\\MetaEditor64.exe"

    def _boom(cmd, **kwargs):
        raise RuntimeError(f"kaboom at {secret}")
    monkeypatch.setattr(compile_handler.subprocess, "run", _boom)
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 500
    text = json.dumps(resp.get_json())
    assert "kaboom" not in text
    assert "RuntimeError" not in text
    assert secret.replace("\\", "\\\\") not in text and "MetaEditor64.exe" not in text


# ── Size limits ──────────────────────────────────────────────────────────────

def test_an_oversized_source_is_rejected_before_anything_is_written(
    client, compile_env, monkeypatch
):
    """The source cap bounds the disk write, so it must fire before the write:
    a 413 that arrives after the temp dir was populated bounds nothing."""
    monkeypatch.setattr(compile_handler, "COMPILE_MAX_SOURCE_BYTES", 1024)
    calls = []
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        lambda *a, **k: calls.append(a) or FakeCompleted(0),
    )
    resp = _post(client, {"source": "x" * 2048})
    assert resp.status_code == 413
    body = resp.get_json()
    assert body["ok"] is False
    assert "1024" in body["log"], "the refusal must name the limit"
    assert calls == [], "the compiler must never see an oversized source"
    assert not compile_env["work"].exists(), "nothing may reach the work dir"


def test_an_oversized_body_is_refused_from_its_declared_length_before_parsing(
    client, compile_env, monkeypatch
):
    """A body over the cap is 413 straight from Content-Length. The payload
    here is not even JSON: a 400 would prove the parser read it, a 413 proves
    it was refused unread."""
    monkeypatch.setattr(compile_handler, "COMPILE_MAX_SOURCE_BYTES", 1024)
    raw = b"x" * (4 * 1024 + 5000)  # over 4*cap + 4096 envelope slack
    resp = client.post(
        "/compile",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {COMPILE_TOKEN}",
        },
    )
    assert resp.status_code == 413
    assert isinstance(resp.get_json()["log"], str)


def test_a_source_within_the_cap_still_compiles(client, compile_env, monkeypatch):
    monkeypatch.setattr(compile_handler, "COMPILE_MAX_SOURCE_BYTES", 1024)
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), ex5_bytes=b"EX5"),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_an_oversized_artifact_is_refused_before_it_is_encoded(
    client, compile_env, monkeypatch
):
    """The output cap bounds memory and response size, so it is checked on
    disk: the refusal must carry no ex5_base64 at all, not a truncated one.
    And it is not a 422 - the caller's source compiled fine; the server is
    declining to return the result."""
    monkeypatch.setattr(compile_handler, "COMPILE_MAX_EX5_BYTES", 1024)
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), ex5_bytes=b"B" * 2048),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert "ex5_base64" not in body
    assert "1024" in body["log"], "the refusal must name the limit"


def test_an_artifact_within_the_cap_is_returned_whole(client, compile_env, monkeypatch):
    payload = b"B" * 512
    monkeypatch.setattr(compile_handler, "COMPILE_MAX_EX5_BYTES", 1024)
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), ex5_bytes=payload),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 200
    assert base64.b64decode(resp.get_json()["ex5_base64"]) == payload


# ── Temp directory hygiene ───────────────────────────────────────────────────

@pytest.mark.parametrize("log_text,ex5,expect_status", [
    (SUCCESS_LOG, b"ex5", 200),
    (ERROR_LOG, None, 422),
])
def test_temp_directory_is_removed_after_success_and_failure(
    client, compile_env, monkeypatch, log_text, ex5, expect_status
):
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(log_text), ex5),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == expect_status
    work = compile_env["work"]
    leftovers = list(work.iterdir()) if work.exists() else []
    assert leftovers == [], f"temp dirs left behind: {leftovers}"


def test_temp_directory_is_removed_after_a_timeout(client, compile_env, monkeypatch):
    def _timeout(cmd, **kwargs):
        raise compile_handler.subprocess.TimeoutExpired(cmd, 30)
    monkeypatch.setattr(compile_handler.subprocess, "run", _timeout)
    _post(client, {"source": "void OnTick(){}"})
    work = compile_env["work"]
    assert (list(work.iterdir()) if work.exists() else []) == []


# ── The compiler invocation itself ───────────────────────────────────────────

def test_caller_controls_neither_the_log_nor_the_include_argument(
    client, compile_env, monkeypatch
):
    recorded = []
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5", record=recorded),
    )
    _post(client, {
        "source": "void OnTick(){}",
        "filename": "ea.mq5",
        "log": "C:\\evil.log",
        "include": "C:\\evil",
    })
    cmd = recorded[0]
    log_arg = next(a for a in cmd if a.startswith("/log:"))
    inc_arg = next(a for a in cmd if a.startswith("/inc:"))
    assert "evil" not in log_arg
    assert "evil" not in inc_arg
    assert inc_arg == f"/inc:{compile_handler.COMPILE_INCLUDE_DIR}"
    # The source compiled is the one we wrote, inside the temp dir.
    src_arg = next(a for a in cmd if a.startswith("/compile:"))
    assert src_arg.endswith("ea.mq5")
    assert str(compile_env["work"]) in src_arg


# ── Auth: a compile-only credential that opens nothing else ──────────────────

def test_compile_accepts_the_compile_token(client, compile_env, monkeypatch):
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )
    assert _post(client, {"source": "void OnTick(){}"}, token=COMPILE_TOKEN).status_code == 200


def test_compile_also_accepts_the_existing_api_token(client, compile_env, monkeypatch):
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )
    assert _post(client, {"source": "void OnTick(){}"}, token=API_TOKEN).status_code == 200


@pytest.mark.parametrize("token", [None, "", "wrong-token", "Bearer-ish"])
def test_compile_rejects_bad_or_missing_credentials(client, compile_env, token):
    resp = _post(client, {"source": "void OnTick(){}"}, token=token)
    assert resp.status_code in (401, 403)
    # Even the auth failure answers JSON: a non-JSON body means "broken host"
    # to this client, which would send it down a different recovery path.
    assert resp.is_json


@pytest.mark.parametrize("method,path", [
    ("get", "/account"),
    ("get", "/positions"),
    ("get", "/orders"),
    ("post", "/orders"),
    ("post", "/terminal/restart"),
    ("get", "/terminal"),
])
def test_compile_token_is_refused_on_every_other_route(client, method, path):
    """The whole point of the second credential.

    The existing token opens order placement, position management and terminal
    restart. A caller that only needs to compile must not be able to reach
    any of it, so the compile token has to fail CLOSED everywhere else — and
    the trade routes are the ones that matter.
    """
    resp = getattr(client, method)(
        path, headers={"Authorization": f"Bearer {COMPILE_TOKEN}"}
    )
    assert resp.status_code == 401, f"{method.upper()} {path} accepted the compile token"


def test_the_full_api_token_still_works_on_other_routes(client, monkeypatch):
    """The existing gate must be unchanged for existing routes."""
    resp = client.get("/ping", headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert resp.status_code == 200


def test_other_routes_still_reject_a_missing_token(client):
    assert client.get("/ping").status_code == 401


# ── Local toolchain mirror ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_toolchain_cache(monkeypatch):
    """The mirror resolves once per process; tests must not inherit each
    other's resolution."""
    monkeypatch.setattr(compile_handler, "_LOCAL_TOOLCHAIN", None)
    monkeypatch.setattr(compile_handler, "_LOCAL_TOOLCHAIN_RESOLVED", False)


def test_no_mirror_configured_uses_the_shared_toolchain(client, compile_env, monkeypatch):
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", "")
    recorded = []
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5", record=recorded),
    )
    _post(client, {"source": "void OnTick(){}"})
    assert recorded[0][0] == compile_env["editor"]


def test_mirror_copies_the_toolchain_and_compiles_from_it(
    client, compile_env, monkeypatch, tmp_path
):
    # The shared "terminal dir" gets an include tree and a Config dir alongside
    # MetaEditor, mirroring the real layout.
    shared = tmp_path
    (shared / "MQL5" / "Include").mkdir(parents=True)
    (shared / "MQL5" / "Include" / "Lib.mqh").write_text("// lib")
    (shared / "Config").mkdir()
    cache = tmp_path / "local-cache"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))

    recorded = []
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5", record=recorded),
    )
    assert _post(client, {"source": "void OnTick(){}"}).status_code == 200

    # Compiled from the mirror, not the shared copy.
    assert recorded[0][0] == str(cache / "MetaEditor64.exe")
    inc = next(a for a in recorded[0] if a.startswith("/inc:"))
    assert inc == f"/inc:{cache / 'MQL5'}"
    # Includes came along, so a compile never reaches back across the mount.
    assert (cache / "MQL5" / "Include" / "Lib.mqh").read_text() == "// lib"
    # And only what a compile needs: no terminal64.exe, no Bases.
    assert not (cache / "terminal64.exe").exists()


def test_mirror_is_built_once_per_process(client, compile_env, monkeypatch, tmp_path):
    cache = tmp_path / "cache-once"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    copies = []
    real_copy = compile_handler.shutil.copy2
    monkeypatch.setattr(
        compile_handler.shutil, "copy2",
        lambda *a, **k: (copies.append(a[0]), real_copy(*a, **k))[1],
    )
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )
    for _ in range(3):
        _post(client, {"source": "void OnTick(){}"})
    # 105MB does not get copied on every request.
    assert len(copies) == 1, f"toolchain copied {len(copies)} times"


def test_a_warm_mirror_is_not_re_copied_on_the_next_process_start(
    client, compile_env, monkeypatch, tmp_path
):
    """The regression that matters after a restart.

    The mirror is built once per PROCESS, but the VM restarts several times a
    day and the mirror survives on local disk. Re-copying the whole tree each
    time puts that cost in front of the first compile after every restart, and
    the stock MQL5 Include tree is ~260 files - big enough on a slow mount to
    push that first caller past a reverse-proxy timeout.
    """
    shared = tmp_path
    include = shared / "MQL5" / "Include" / "Trade"
    include.mkdir(parents=True)
    for name in ("Trade.mqh", "SymbolInfo.mqh", "PositionInfo.mqh"):
        (include / name).write_text(f"// {name}")
    (shared / "Config").mkdir()
    cache = tmp_path / "warm-cache"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )

    # First process: builds the mirror.
    assert _post(client, {"source": "void OnTick(){}"}).status_code == 200
    assert (cache / "MQL5" / "Include" / "Trade" / "Trade.mqh").exists()

    # Second process, same on-disk mirror: nothing should be copied again.
    compile_handler._LOCAL_TOOLCHAIN = None
    compile_handler._LOCAL_TOOLCHAIN_RESOLVED = False
    copies = []
    real_copy = compile_handler.shutil.copy2
    monkeypatch.setattr(
        compile_handler.shutil, "copy2",
        lambda *a, **k: (copies.append(a[0]), real_copy(*a, **k))[1],
    )
    assert _post(client, {"source": "void OnTick(){}"}).status_code == 200
    assert copies == [], f"re-copied {len(copies)} unchanged file(s) on restart"


def test_a_changed_include_is_still_picked_up_by_the_mirror(
    client, compile_env, monkeypatch, tmp_path
):
    """Skipping unchanged files must not mean serving a stale library.

    An edited .mqh that compiles clean but is a version behind is worse than a
    slow compile, so the staleness check has to actually notice the edit.
    """
    shared = tmp_path
    include = shared / "MQL5" / "Include"
    include.mkdir(parents=True)
    (include / "Lib.mqh").write_text("// v1")
    (shared / "Config").mkdir()
    cache = tmp_path / "refresh-cache"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )
    _post(client, {"source": "void OnTick(){}"})
    assert (cache / "MQL5" / "Include" / "Lib.mqh").read_text() == "// v1"

    # Edit the source library, then restart. Size and mtime both move.
    (include / "Lib.mqh").write_text("// v2 is longer than v1")
    os.utime(include / "Lib.mqh", (time.time() + 10, time.time() + 10))
    compile_handler._LOCAL_TOOLCHAIN = None
    compile_handler._LOCAL_TOOLCHAIN_RESOLVED = False

    _post(client, {"source": "void OnTick(){}"})
    assert (cache / "MQL5" / "Include" / "Lib.mqh").read_text() == "// v2 is longer than v1"


def test_a_broken_mirror_falls_back_instead_of_failing_the_compile(
    client, compile_env, monkeypatch, tmp_path
):
    # A compile that works slowly beats one that stops working because a cache
    # directory was not writable.
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(tmp_path / "nope"))
    monkeypatch.setattr(
        compile_handler.shutil, "copy2",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    recorded = []
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5", record=recorded),
    )
    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 200
    assert recorded[0][0] == compile_env["editor"]


def test_an_edited_include_reaches_later_compiles_without_a_restart(
    client, compile_env, monkeypatch, tmp_path
):
    """A shared header is edited while the process keeps running.

    The mirror is otherwise resolved once per process, so without a periodic
    re-check the old copy is used until the next restart - and the compile that
    used it still returns ok:true. A silently stale binary is the worst outcome
    available here: nothing downstream can distinguish it from a correct one.
    """
    shared = tmp_path
    include = shared / "MQL5" / "Include"
    include.mkdir(parents=True)
    (include / "Lib.mqh").write_text("// v1")
    (shared / "Config").mkdir()
    cache = tmp_path / "refresh-live"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    monkeypatch.setattr(compile_handler, "INCLUDE_REFRESH_SECONDS", 0)
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )

    _post(client, {"source": "void OnTick(){}"})
    assert (cache / "MQL5" / "Include" / "Lib.mqh").read_text() == "// v1"

    # Edit the shared header. No restart, no cache reset.
    (include / "Lib.mqh").write_text("// v2 and longer")
    os.utime(include / "Lib.mqh", (time.time() + 5, time.time() + 5))

    _post(client, {"source": "void OnTick(){}"})
    assert (cache / "MQL5" / "Include" / "Lib.mqh").read_text() == "// v2 and longer"


def test_the_include_recheck_is_rate_limited(client, compile_env, monkeypatch, tmp_path):
    """The re-check walks the source tree, which sits on the slow mount.

    Doing that on every compile would put the walk in front of every caller, so
    it is bounded by INCLUDE_REFRESH_SECONDS rather than run each time.
    """
    shared = tmp_path
    (shared / "MQL5" / "Include").mkdir(parents=True)
    (shared / "MQL5" / "Include" / "Lib.mqh").write_text("// v1")
    (shared / "Config").mkdir()
    cache = tmp_path / "ratelimit"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    monkeypatch.setattr(compile_handler, "INCLUDE_REFRESH_SECONDS", 3600)
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )
    _post(client, {"source": "void OnTick(){}"})

    walks = []
    real_walk = compile_handler.os.walk
    monkeypatch.setattr(
        compile_handler.os, "walk",
        lambda *a, **k: (walks.append(a[0]), real_walk(*a, **k))[1],
    )
    for _ in range(5):
        _post(client, {"source": "void OnTick(){}"})
    assert walks == [], f"re-walked the include tree {len(walks)} time(s) inside the window"


# ── include_hash: which library was this binary built against? ───────────────

def _hash_env(monkeypatch, tmp_path, name="hash-cache"):
    """Shared toolchain + mirror, with one header in the include tree."""
    shared = tmp_path
    include = shared / "MQL5" / "Include"
    include.mkdir(parents=True, exist_ok=True)
    (include / "Lib.mqh").write_text("// v1")
    (shared / "Config").mkdir(exist_ok=True)
    cache = tmp_path / name
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    monkeypatch.setattr(compile_handler, "INCLUDE_REFRESH_SECONDS", 0)
    monkeypatch.setattr(compile_handler, "_INCLUDE_HASH", None)
    monkeypatch.setattr(compile_handler, "_INCLUDE_HASH_KEY", None)
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), b"ex5"),
    )
    return include


def test_success_reports_the_include_hash(client, compile_env, monkeypatch, tmp_path):
    _hash_env(monkeypatch, tmp_path)
    body = _post(client, {"source": "void OnTick(){}"}).get_json()
    assert body["include_hash"].startswith("sha256:")
    assert len(body["include_hash"]) == len("sha256:") + 64


def test_the_include_hash_is_stable_across_compiles(client, compile_env, monkeypatch, tmp_path):
    """Two builds against an unchanged library must be comparable."""
    _hash_env(monkeypatch, tmp_path)
    first = _post(client, {"source": "void OnTick(){}"}).get_json()["include_hash"]
    second = _post(client, {"source": "void OnTick(){}"}).get_json()["include_hash"]
    assert first == second


def test_editing_a_header_changes_the_include_hash(client, compile_env, monkeypatch, tmp_path):
    """The whole point: drift has to be detectable after the fact."""
    include = _hash_env(monkeypatch, tmp_path)
    before = _post(client, {"source": "void OnTick(){}"}).get_json()["include_hash"]

    (include / "Lib.mqh").write_text("// v2 is different")
    os.utime(include / "Lib.mqh", (time.time() + 5, time.time() + 5))

    after = _post(client, {"source": "void OnTick(){}"}).get_json()["include_hash"]
    assert after != before, "an edited header left the include hash unchanged"


def test_adding_a_header_changes_the_include_hash(client, compile_env, monkeypatch, tmp_path):
    """Contents alone would miss this - the digest covers paths too."""
    include = _hash_env(monkeypatch, tmp_path)
    before = _post(client, {"source": "void OnTick(){}"}).get_json()["include_hash"]

    (include / "Extra.mqh").write_text("// new")
    after = _post(client, {"source": "void OnTick(){}"}).get_json()["include_hash"]
    assert after != before, "a new header left the include hash unchanged"


def test_the_hash_describes_the_mirror_not_the_source(client, compile_env, monkeypatch, tmp_path):
    """If the mirror is stale, the hash must report what was COMPILED.

    Hashing the source instead would assert the build used a library it did not,
    which is worse than reporting no hash at all.
    """
    include = _hash_env(monkeypatch, tmp_path, name="mirror-truth")
    body = _post(client, {"source": "void OnTick(){}"}).get_json()
    mirrored = body["include_hash"]

    # Change the source but freeze the mirror by disabling any further refresh.
    monkeypatch.setattr(compile_handler, "INCLUDE_REFRESH_SECONDS", 3600)
    monkeypatch.setattr(compile_handler, "_INCLUDES_CHECKED_AT", time.monotonic())
    (include / "Lib.mqh").write_text("// source moved on without the mirror")
    os.utime(include / "Lib.mqh", (time.time() + 9, time.time() + 9))

    again = _post(client, {"source": "void OnTick(){}"}).get_json()["include_hash"]
    assert again == mirrored, "hash followed the source instead of the compiled tree"


# ── Boot warm-up ─────────────────────────────────────────────────────────────
#
# The warm-up exists to move MetaEditor's cold load off the first real caller.
# Its gates matter more than the compile it runs: ungated, every API process on
# the VM launches its own MetaEditor at boot.

def test_warmup_does_nothing_without_a_local_cache(monkeypatch):
    """No mirror means no cold-load problem worth a background compile."""
    monkeypatch.setattr(compile_handler, "WARMUP_ENABLED", False)
    started = []
    monkeypatch.setattr(
        compile_handler.threading, "Thread",
        lambda *a, **k: started.append(k) or pytest.fail("started a thread"),
    )
    compile_handler.start_warmup()
    assert started == []


def test_only_one_process_claims_the_warmup(monkeypatch, tmp_path):
    """The regression that matters.

    Every API process on the VM runs this code and they share the cache dir, so
    the claim has to be exclusive ACROSS processes - twenty MetaEditors starting
    together would recreate the CPU saturation the warm-up is meant to avoid.
    """
    cache = tmp_path / "claim"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    winners = [compile_handler._claim_warmup() for _ in range(20)]
    assert winners.count(True) == 1, f"{winners.count(True)} processes claimed the warm-up"


def test_an_abandoned_claim_does_not_disable_warmup_forever(monkeypatch, tmp_path):
    """The cache survives reboots, so the claim inside it must not be permanent.

    A process killed mid-warm-up leaves the file behind; without expiry that
    would silently disable warm-up on this VM for good.
    """
    cache = tmp_path / "stale"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    assert compile_handler._claim_warmup() is True
    assert compile_handler._claim_warmup() is False

    claim = cache / compile_handler._WARMUP_CLAIM
    old = time.time() - (compile_handler.WARMUP_CLAIM_TTL_SECONDS + 60)
    os.utime(claim, (old, old))

    assert compile_handler._claim_warmup() is True, "an expired claim still blocked warm-up"


def test_warmup_yields_to_a_real_compile(monkeypatch, tmp_path):
    """A caller must never queue behind a warm-up - that inverts its purpose."""
    cache = tmp_path / "yield"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    monkeypatch.setattr(compile_handler, "WARMUP_DELAY_SECONDS", 0)
    ran = []
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        lambda *a, **k: ran.append(a) or FakeCompleted(0),
    )

    compile_handler._COMPILE_LOCK.acquire()  # stand in for a compile in flight
    try:
        compile_handler._warmup()
    finally:
        compile_handler._COMPILE_LOCK.release()

    assert ran == [], "warm-up ran MetaEditor while a compile held the lock"


def test_a_failing_warmup_is_swallowed(monkeypatch, tmp_path):
    """Warm-up is an optimisation. It must never take the process down."""
    cache = tmp_path / "boom"
    monkeypatch.setattr(compile_handler, "COMPILE_LOCAL_CACHE", str(cache))
    monkeypatch.setattr(compile_handler, "WARMUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(compile_handler, "COMPILE_METAEDITOR", str(tmp_path / "me.exe"))
    (tmp_path / "me.exe").write_bytes(b"MZ")
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("cannot launch")),
    )
    compile_handler._warmup()  # must not raise
    # And the lock is released, so real compiles still work afterwards.
    assert compile_handler._COMPILE_LOCK.acquire(blocking=False)
    compile_handler._COMPILE_LOCK.release()


def test_a_header_deleted_from_source_is_pruned_from_the_mirror(
    client, compile_env, monkeypatch, tmp_path
):
    """Copy-only leaves a one-way mirror.

    A header removed from the source stayed in the mirror and kept resolving,
    so `#include <Gone.mqh>` still compiled against a file nobody maintains.
    Found in production: a test header deleted from the source was still being
    included by the compiler hours later.
    """
    include = _hash_env(monkeypatch, tmp_path, name="prune")
    (include / "Doomed.mqh").write_text("// remove me")
    cache = tmp_path / "prune"
    _post(client, {"source": "void OnTick(){}"})
    assert (cache / "MQL5" / "Include" / "Doomed.mqh").exists()

    os.remove(include / "Doomed.mqh")
    _post(client, {"source": "void OnTick(){}"})

    assert not (cache / "MQL5" / "Include" / "Doomed.mqh").exists(), \
        "a header deleted from source survived in the mirror"


def test_pruning_is_skipped_when_the_source_is_unreachable(monkeypatch, tmp_path):
    """A transient mount failure must not wipe the mirror.

    The source walk yields nothing when the share is down; pruning against that
    empty set would delete every mirrored header and turn a blip into an outage.
    """
    src = tmp_path / "gone"          # never created
    dst = tmp_path / "mirror"
    (dst / "Include").mkdir(parents=True)
    (dst / "Include" / "Keep.mqh").write_text("// precious")

    copied, removed = compile_handler._mirror_tree(str(src), str(dst))

    assert (copied, removed) == (0, 0)
    assert (dst / "Include" / "Keep.mqh").exists(), "pruned the mirror against an empty source"


# ── include_files: WHICH header moved, not just that something did ───────────

def _digest_env(monkeypatch, tmp_path, patterns="Mine*.mqh", name="perfile"):
    include = _hash_env(monkeypatch, tmp_path, name=name)
    (include / "MineLicense.mqh").write_text("// licence v1")
    (include / "Stock.mqh").write_text("// vendor library")
    monkeypatch.setattr(
        compile_handler, "_INCLUDE_DIGEST_PATTERNS",
        tuple(p.strip() for p in patterns.split(",") if p.strip()),
    )
    monkeypatch.setattr(compile_handler, "_INCLUDE_FILES", None)
    return include


def test_include_files_reports_only_the_configured_headers(
    client, compile_env, monkeypatch, tmp_path
):
    _digest_env(monkeypatch, tmp_path)
    body = _post(client, {"source": "void OnTick(){}"}).get_json()
    assert set(body["include_files"]) == {"MineLicense.mqh"}
    assert body["include_files"]["MineLicense.mqh"].startswith("sha256:")


def test_include_files_is_absent_when_nothing_is_configured(
    client, compile_env, monkeypatch, tmp_path
):
    """Default must stay off: ~260 digests per response is a payload, not an answer."""
    _digest_env(monkeypatch, tmp_path, patterns="", name="unconfigured")
    body = _post(client, {"source": "void OnTick(){}"}).get_json()
    assert "include_files" not in body
    assert body["include_hash"].startswith("sha256:")


def test_a_stock_library_change_moves_the_tree_hash_but_not_our_header(
    client, compile_env, monkeypatch, tmp_path
):
    """The whole point of the field.

    A MetaTrader upgrade and a licence-header edit both move the tree hash. If
    the per-file digest is unchanged, the caller knows the second did not
    happen and can skip rebuilding every dependent artifact.
    """
    include = _digest_env(monkeypatch, tmp_path, name="stockmove")
    first = _post(client, {"source": "void OnTick(){}"}).get_json()

    (include / "Stock.mqh").write_text("// vendor library, upgraded and longer")
    os.utime(include / "Stock.mqh", (time.time() + 5, time.time() + 5))
    second = _post(client, {"source": "void OnTick(){}"}).get_json()

    assert second["include_hash"] != first["include_hash"], "tree hash missed a change"
    assert second["include_files"] == first["include_files"], \
        "a stock-library change moved our header's digest"


def test_editing_our_header_moves_its_own_digest(client, compile_env, monkeypatch, tmp_path):
    include = _digest_env(monkeypatch, tmp_path, name="ourmove")
    first = _post(client, {"source": "void OnTick(){}"}).get_json()

    (include / "MineLicense.mqh").write_text("// licence v2, materially different")
    os.utime(include / "MineLicense.mqh", (time.time() + 5, time.time() + 5))
    second = _post(client, {"source": "void OnTick(){}"}).get_json()

    assert second["include_files"]["MineLicense.mqh"] != first["include_files"]["MineLicense.mqh"]


def test_a_configured_header_missing_from_the_tree_is_absent_not_null(
    client, compile_env, monkeypatch, tmp_path
):
    """Absent means "not in the tree the compiler read" - the thing worth
    knowing before a rebuild. A null would blur that with "unreadable"."""
    _digest_env(monkeypatch, tmp_path, patterns="Mine*.mqh,NeverExisted.mqh", name="absent")
    body = _post(client, {"source": "void OnTick(){}"}).get_json()
    assert "NeverExisted.mqh" not in body["include_files"]
    assert body["include_files"]["MineLicense.mqh"].startswith("sha256:")


# ── Cross-process compile serialization ───────────────────────────────────
#
# MetaEditor is single-instance per installation directory, but _COMPILE_LOCK
# is a threading.Lock: it only serializes calls inside ONE process. Every
# mt5api process on a VM exposes /compile and, by default, all of them
# resolve to the SAME installation directory -- verified live (deep-qa audit)
# by running two real, separate OS processes against a stand-in MetaEditor
# sharing one directory: with no cross-process lock they ran fully
# concurrently (identical START/END timestamps); with it, they serialized
# (zero time overlap). These pin the mechanism at the unit level.

def test_cross_process_lock_is_exclusive(tmp_path):
    editor = str(tmp_path / "MetaEditor64.exe")
    far_future = time.monotonic() + 60

    held = compile_handler._acquire_cross_process_lock(editor, far_future)
    assert held is not None

    # Same scope, already held: a near-past deadline must fail fast, not
    # block for the full 60s budget above.
    blocked = compile_handler._acquire_cross_process_lock(editor, time.monotonic())
    assert blocked is None

    compile_handler._release_cross_process_lock(held)

    reacquired = compile_handler._acquire_cross_process_lock(editor, far_future)
    assert reacquired is not None
    compile_handler._release_cross_process_lock(reacquired)


def test_cross_process_lock_scope_is_the_installation_directory(tmp_path):
    """Two DIFFERENT installation directories must not contend with each
    other -- only processes sharing the same one are the actual risk."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    editor_a = str(tmp_path / "a" / "MetaEditor64.exe")
    editor_b = str(tmp_path / "b" / "MetaEditor64.exe")

    held_a = compile_handler._acquire_cross_process_lock(editor_a, time.monotonic() + 5)
    held_b = compile_handler._acquire_cross_process_lock(editor_b, time.monotonic())
    assert held_a is not None
    assert held_b is not None, "an unrelated installation directory was blocked"
    compile_handler._release_cross_process_lock(held_a)
    compile_handler._release_cross_process_lock(held_b)


def test_an_abandoned_cross_process_lock_does_not_wedge_the_endpoint_forever(tmp_path):
    """A process killed mid-compile leaves the lock file behind; without
    expiry every future compile from every process sharing this install
    would report 'busy' forever."""
    editor = str(tmp_path / "MetaEditor64.exe")
    held = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 5)
    assert held is not None

    old = time.time() - (compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS + 60)
    os.utime(held.path, (old, old))

    reacquired = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 5)
    assert reacquired is not None, "an expired cross-process lock still blocked a compile"
    compile_handler._release_cross_process_lock(reacquired)


def test_a_real_compile_waits_for_the_cross_process_lock_then_proceeds(
    client, compile_env, monkeypatch
):
    """HTTP-level: proves POST /compile itself engages the cross-process lock
    (not just that the helper functions work in isolation)."""
    monkeypatch.setattr(compile_handler.subprocess, "run", _fake_metaeditor(_utf16_log(SUCCESS_LOG), ex5_bytes=b"binary"))

    held = compile_handler._acquire_cross_process_lock(compile_env["editor"], time.monotonic() + 60)
    assert held is not None

    result = {}

    def _call():
        result["response"] = _post(client, {"source": "void OnTick(){}"})

    t = threading.Thread(target=_call)
    t.start()
    t.join(timeout=1)
    assert t.is_alive(), "the request completed without waiting for the held cross-process lock"

    compile_handler._release_cross_process_lock(held)
    t.join(timeout=5)
    assert not t.is_alive()

    body = result["response"].get_json()
    assert result["response"].status_code == 200
    assert body["ok"] is True


def test_compile_returns_504_when_the_cross_process_lock_never_frees(
    client, compile_env, monkeypatch
):
    monkeypatch.setattr(compile_handler, "COMPILE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(compile_handler, "_LOCK_WAIT_MARGIN_SECONDS", 0)

    held = compile_handler._acquire_cross_process_lock(compile_env["editor"], time.monotonic() + 60)
    assert held is not None
    try:
        resp = _post(client, {"source": "void OnTick(){}"})
        assert resp.status_code == 504
        assert "compiler lock" in resp.get_json()["log"]
    finally:
        compile_handler._release_cross_process_lock(held)


def test_slow_toolchain_resolution_extends_the_lock_deadline_instead_of_starving_it(
    client, compile_env, monkeypatch
):
    """_local_toolchain() runs (and, on a first call with a mirror configured,
    can take real wall-clock time copying ~100MB over a slow mount) AFTER
    `deadline` is computed in _compile_source_inner, but BEFORE the
    cross-process lock is acquired in _run_compile. Without extending
    `deadline` by however long that took, a slow-but-otherwise-uncontended
    mirror build could burn the whole lock-wait budget by itself and produce
    a spurious 504 that was never actual lock contention -- counter-review
    finding from the deep-qa audit that added the cross-process lock.

    The lock must be genuinely CONTENDED to prove this: an uncontended
    O_CREAT|O_EXCL acquires on its very first attempt regardless of how much
    of `deadline` is left (even a deadline already in the past), since the
    deadline is only ever consulted after a FAILED attempt. So this holds the
    lock in a background thread, releasing it only after toolchain
    resolution's slow stub has already run — the extension is what buys the
    remaining wait for that release.
    """
    monkeypatch.setattr(compile_handler, "COMPILE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(compile_handler, "_LOCK_WAIT_MARGIN_SECONDS", 0)  # total budget: 1s

    held = compile_handler._acquire_cross_process_lock(
        compile_env["editor"], time.monotonic() + 10
    )
    assert held is not None

    def _release_after_delay():
        time.sleep(1.5)  # after the 1.2s toolchain stub below finishes
        compile_handler._release_cross_process_lock(held)

    threading.Thread(target=_release_after_delay, daemon=True).start()

    real_local_toolchain = compile_handler._local_toolchain

    def _slow_local_toolchain():
        time.sleep(1.2)  # alone, already exceeds the 1s total budget
        return real_local_toolchain()

    monkeypatch.setattr(compile_handler, "_local_toolchain", _slow_local_toolchain)
    monkeypatch.setattr(
        compile_handler.subprocess, "run",
        _fake_metaeditor(_utf16_log(SUCCESS_LOG), ex5_bytes=b"binary"),
    )

    resp = _post(client, {"source": "void OnTick(){}"})
    assert resp.status_code == 200, resp.get_json()


def test_cross_process_lock_logs_unexpected_errors_not_just_contention(monkeypatch, tmp_path):
    """A FileExistsError from a held lock is ordinary and silent. Anything
    else (permission denied, a bad path, ENOSPC) means the lock mechanism
    itself is broken and every compile would silently report generic "busy"
    forever -- that must be visible in the logs, not indistinguishable from
    ordinary contention.
    """
    editor = str(tmp_path / "MetaEditor64.exe")
    warnings = []
    monkeypatch.setattr(
        compile_handler.log, "warning", lambda *a, **k: warnings.append((a, k))
    )

    lock_path = compile_handler._cross_process_lock_path(editor)
    real_open = os.open

    def _flaky_open(path, flags, mode=0o777):
        if path == lock_path:
            raise PermissionError("simulated: lock dir not writable")
        return real_open(path, flags, mode)

    monkeypatch.setattr(compile_handler.os, "open", _flaky_open)

    result = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 0.3)

    assert result is None
    assert warnings, "no warning logged for a non-contention OSError during lock acquisition"
    assert any("unusable" in str(args) for args, _kwargs in warnings)


# ── Ownership: the race psyb0t reproduced on the first revision ──────
#
# The stale window was COMPILE_TIMEOUT_SECONDS + 120, identical to the warm-up
# budget, so a slow-but-live holder could be declared stale mid-compile. A
# second process then reaped that lock and created its own -- and the first
# holder's release unlinked the path unconditionally, deleting the SECOND
# holder's lock and letting a third compiler in alongside it.


def test_a_late_holder_does_not_delete_the_lock_that_replaced_its_own(tmp_path):
    """The exact sequence from the review: acquire, force-stale, let a second
    owner take it, then release the first. The first holder must NOT remove
    the second owner's lock, and a third caller must still be shut out.
    """
    editor = str(tmp_path / "MetaEditor64.exe")

    first = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 5)
    assert first is not None

    # Force the false-stale condition the old stale window allowed.
    old = time.time() - (compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS + 60)
    os.utime(first.path, (old, old))

    second = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 5)
    assert second is not None, "the stale reaper should still recover an abandoned lock"
    assert second.token != first.token

    # The late holder finishes and releases. This must be a no-op on someone
    # else's lock.
    compile_handler._release_cross_process_lock(first)

    assert os.path.exists(second.path), "the late holder deleted the live owner's lock"
    assert compile_handler._read_lock_token(second.path) == second.token

    third = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 0.5)
    assert third is None, "a third compiler entered while the second was still live"

    compile_handler._release_cross_process_lock(second)


def test_a_live_holder_is_not_reaped_as_stale_while_it_works(tmp_path):
    """The other half: a holder that outlives the stale window keeps its lock,
    because its heartbeat keeps the mtime moving. Without that, exclusion
    depends on a compile finishing faster than a fixed guess."""
    editor = str(tmp_path / "MetaEditor64.exe")
    # A stale window shorter than the hold, and a heartbeat inside it.
    monkeypatch_stale = compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS
    monkeypatch_beat = compile_handler._CROSS_PROCESS_LOCK_HEARTBEAT_SECONDS
    compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS = 5
    compile_handler._CROSS_PROCESS_LOCK_HEARTBEAT_SECONDS = 0.25
    try:
        held = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 5)
        assert held is not None
        time.sleep(7)  # comfortably longer than the stale window

        blocked = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 0.3)
        assert blocked is None, "a live holder was reaped as stale despite its heartbeat"

        compile_handler._release_cross_process_lock(held)
    finally:
        compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS = monkeypatch_stale
        compile_handler._CROSS_PROCESS_LOCK_HEARTBEAT_SECONDS = monkeypatch_beat


# ── Real multi-process mutual exclusion ──────────────────────────────


def _run_lock_race(tmp_path, monkeypatch, workers, hold, pre_stale):
    """Start `workers` real interpreters that all grab the compile lock at once,
    and return their (pid, entered, exited) intervals."""
    ctx = multiprocessing.get_context("spawn")
    editor = str(tmp_path / "MetaEditor64.exe")

    if pre_stale:
        # A lock left behind by a process that died mid-compile. Every worker
        # will judge it stale in the same instant.
        lock_path = compile_handler._cross_process_lock_path(editor)
        with open(lock_path, "w", encoding="ascii") as handle:
            handle.write("deadbeef" * 4)
        old = time.time() - (compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS + 60)
        os.utime(lock_path, (old, old))

    monkeypatch.setenv("MT5_REPO_ROOT", str(pathlib.Path(__file__).resolve().parents[1]))
    with ctx.Manager() as manager:
        results = manager.list()
        barrier = manager.Barrier(workers)
        procs = [
            ctx.Process(
                target=compile_lock_worker.run,
                args=(editor, hold, barrier, results),
            )
            for _ in range(workers)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=180)
            assert proc.exitcode == 0, f"worker exited {proc.exitcode}"
        intervals = sorted(list(results), key=lambda row: row[1])

    assert len(intervals) == workers
    assert all(row[0] != "timeout" for row in intervals), f"a worker never got the lock: {intervals}"
    return intervals


def _assert_no_overlap(intervals, hold):
    for (pid_a, _start_a, end_a), (pid_b, start_b, _end_b) in zip(intervals, intervals[1:]):
        assert end_a <= start_b, (
            f"pid {pid_a} still held the lock when pid {pid_b} entered "
            f"({end_a:.3f} > {start_b:.3f}) -- two MetaEditors could run at once"
        )
    span = intervals[-1][2] - intervals[0][1]
    assert span >= hold * len(intervals) * 0.9, f"workers overlapped: span {span:.2f}s"


def test_separate_processes_never_hold_the_compile_lock_at_the_same_time(
    tmp_path, monkeypatch
):
    """The cross-process proof the review asked for: real OS processes, started
    together on a barrier, each recording when it entered and left the critical
    section. Any overlap means two MetaEditors could have run concurrently.

    A helper-level test cannot show this -- it shares one interpreter, so it
    proves nothing about the file lock that is the actual mechanism.
    """
    hold = 0.4
    intervals = _run_lock_race(tmp_path, monkeypatch, workers=4, hold=hold, pre_stale=False)
    _assert_no_overlap(intervals, hold)


def test_separate_processes_do_not_all_win_a_stale_lock_at_once(tmp_path, monkeypatch):
    """The interleaving a bare `os.remove` stale sweep gets wrong.

    Every waiter judges the dead lock stale in the same instant. With a plain
    remove, a loser's unlink lands AFTER a winner has recreated the file --
    deleting the new owner's lock, so the next waiter creates its own and two
    compilers run together. Ownership tokens alone do not catch this: the
    damage happens during acquisition, long before anyone releases.
    """
    hold = 0.4
    intervals = _run_lock_race(tmp_path, monkeypatch, workers=6, hold=hold, pre_stale=True)
    _assert_no_overlap(intervals, hold)


def test_a_stale_sweep_does_not_delete_the_lock_that_replaced_the_dead_one(
    tmp_path, monkeypatch
):
    """The reap-side half of the race, forced deterministically.

    Every waiter judges a dead lock stale in the same instant. The dangerous
    interleaving is a loser that is descheduled between judging and acting: by
    the time it acts, a winner has already reaped and created its own lock, and
    a bare `os.remove` deletes THAT -- so the next waiter walks straight in and
    two MetaEditors run together. Ownership tokens cannot catch it, because the
    damage is done during acquisition, before anyone releases.

    Scheduling this by luck is unreliable (a plain multi-process race reproduces
    it only sometimes), so the window is held open explicitly.
    """
    editor = str(tmp_path / "MetaEditor64.exe")
    lock_path = compile_handler._cross_process_lock_path(editor)

    with open(lock_path, "w", encoding="ascii") as handle:
        handle.write("deadholder")
    stale = time.time() - (compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS + 60)
    os.utime(lock_path, (stale, stale))

    judged = threading.Event()
    winner_done = threading.Event()
    real_getmtime = os.path.getmtime

    def _getmtime_that_holds_the_window_open(path):
        if path == lock_path and not judged.is_set():
            judged.set()
            winner_done.wait(timeout=10)
            return stale  # what the loser saw when it decided to reap
        return real_getmtime(path)

    monkeypatch.setattr(compile_handler.os.path, "getmtime", _getmtime_that_holds_the_window_open)

    loser = threading.Thread(
        target=compile_handler._reap_if_stale, args=(lock_path, "loser-token"), daemon=True
    )
    loser.start()
    assert judged.wait(timeout=10), "the reaper never judged the lock"

    # The winner reaps the dead lock and takes ownership while the loser is
    # still mid-decision.
    os.remove(lock_path)
    with open(lock_path, "w", encoding="ascii") as handle:
        handle.write("winner-token")
    winner_done.set()
    loser.join(timeout=10)

    assert os.path.exists(lock_path), "the stale sweep deleted the new owner's lock"
    assert compile_handler._read_lock_token(lock_path) == "winner-token", (
        "the new owner's lock was replaced by the stale sweep"
    )


def test_an_unreadable_abandoned_lock_is_still_reaped(tmp_path, monkeypatch):
    """A lock file that exists but cannot be READ must not wedge the endpoint.

    Ownership checks read the token, and an unreadable file yields no token --
    so a sweep that bails out whenever it cannot identify the holder never
    reaps this one, and every future compile from every process sharing the
    install returns 504 forever. stat and unlink need no read permission, so
    an unreadable lock older than the stale window is abandoned by definition
    and must still be recoverable.
    """
    editor = str(tmp_path / "MetaEditor64.exe")
    lock_path = compile_handler._cross_process_lock_path(editor)
    with open(lock_path, "w", encoding="ascii") as handle:
        handle.write("deadholder")
    old = time.time() - (compile_handler._CROSS_PROCESS_LOCK_STALE_SECONDS + 600)
    os.utime(lock_path, (old, old))

    real_open = builtins.open

    def _unreadable(path, *args, **kwargs):
        # Keyed on the prefix, not the exact name: permissions follow the
        # inode, so the file stays unreadable after the sweep renames it aside.
        if str(path).startswith(lock_path) and (not args or "r" in str(args[0])):
            raise PermissionError("simulated: lock file not readable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _unreadable)

    assert compile_handler._read_lock_token(lock_path) is None
    acquired = compile_handler._acquire_cross_process_lock(editor, time.monotonic() + 5)

    assert acquired is not None, "an unreadable abandoned lock wedged the endpoint forever"
    compile_handler._release_cross_process_lock(acquired)
