"""The file API on the running stack: REST and the unified MCP tools.

The library case end to end: unzip a two-file library into the compile tree,
compile an expert that includes it, delete the library, and compile again.
The second compile must fail, which proves the first one used the uploaded
files and that /compile notices a change made through the file API.

Needs files enabled on the target terminal and, for the steps that write,
a demo account unless MT5_LIVE_ALLOW_REAL=1 (see conftest).
"""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from tests.live.conftest import ARTIFACT_PREFIX
from tests.live.live_client import McpClient, RestClient, b64

_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_COMPILE_TIMEOUT_SECONDS = 180

LIB_CORE = b"""
#include "Util.mqh"
int LiveTestTwice(int value) { return LiveTestAdd(value, value); }
"""
LIB_UTIL = b"int LiveTestAdd(int a, int b) { return a + b; }\n"
EXPERT_TEMPLATE = """
#include <{library}/Core.mqh>
int OnInit() {{ Print(LiveTestTwice(21)); return(INIT_SUCCEEDED); }}
void OnTick() {{}}
"""


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _compile(rest: RestClient, source: str) -> dict:
    resp = rest.session.post(
        rest.base + "/compile",
        json={"source": source, "filename": "livetest_files.mq5"},
        timeout=_COMPILE_TIMEOUT_SECONDS,
    )
    if resp.status_code == _HTTP_NOT_FOUND:
        pytest.skip("POST /compile is not available on the target")
    return resp.json()


@pytest.fixture
def scratch() -> str:
    return f"{ARTIFACT_PREFIX}{uuid.uuid4().hex[:8]}"


def test_the_terminal_root_lists_with_access_flags(files_enabled_target, rest: RestClient):
    listing = rest.get("/files")

    entries = {entry["name"].lower(): entry for entry in listing["entries"]}
    assert entries["terminal64.exe"]["writable"] is False
    assert entries["mql5"]["type"] == "dir"


def test_credentials_are_never_served(files_enabled_target, rest: RestClient):
    for path in ("mt5start.ini", "Config/accounts.dat"):
        resp = rest.session.get(f"{rest.base}/files/{path}", timeout=60)
        assert resp.status_code == _HTTP_FORBIDDEN, path
        assert "Password" not in resp.text


def test_a_file_round_trips_through_the_mcp_tools(files_api: McpClient, scratch: str):
    path = f"MQL5/Files/{scratch}/state.json"

    put = files_api.call_json("put_file", path=path, content='{"probe": 1}')
    got = files_api.call_json("get_file", path=path)
    listed = files_api.call_json("list_files", path=f"MQL5/Files/{scratch}")
    deleted = files_api.call_json("delete_file", path=f"MQL5/Files/{scratch}", recursive=True)

    assert put["created"] is True
    assert got["text"] == '{"probe": 1}'
    assert [entry["name"] for entry in listed["entries"]] == ["state.json"]
    assert deleted["type"] == "dir"


def test_a_zipped_library_compiles_in_and_its_removal_is_noticed(
    files_api: McpClient,
    rest: RestClient,
    scratch: str,
):
    library = f"Include/{scratch}"
    archive = _zip({"Core.mqh": LIB_CORE, "Util.mqh": LIB_UTIL})
    source = EXPERT_TEMPLATE.format(library=scratch)

    unpacked = files_api.call_json(
        "put_file", path=library, content_base64=b64(archive), extract=True, tree="compile",
    )
    assert unpacked["count"] == 2

    with_library = _compile(rest, source)
    files_api.call_json("delete_file", path=library, recursive=True, tree="compile")
    without_library = _compile(rest, source)

    assert with_library["ok"], with_library.get("log", "")[-1500:]
    assert with_library["ex5_base64"]
    assert not without_library["ok"], "the compile still found the deleted library"
