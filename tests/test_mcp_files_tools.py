"""File API MCP tools (list_files, get_file, put_file, delete_file), on both
MCP servers.

Same arrangement as tests/test_mcp_chartctl_tools.py: the per-terminal server
dispatches in-process, the unified server goes over httpx with a transport
that hands each request to the same Flask app, and both reach the real file
handlers on the shared ``files_app`` fixture.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import zipfile
from typing import Any

import pytest
from flask import Flask
from mcp.server.fastmcp.exceptions import ToolError

from tests.conftest import FILE_TREE_SECRET
from tests.test_mcp_chartctl_tools import (
    ACCOUNT,
    BROKER,
    TERMINAL_KEY,
    _content_blocks,
    _FlaskTransport,
    _REJECTION,
)

FILE_TOOLS = frozenset({"list_files", "get_file", "put_file", "delete_file"})
_TOOL_HTTP_ERROR = re.compile(r"HTTP (\d+): (.*)$", re.DOTALL)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class _TerminalHarness:
    def __init__(self, monkeypatch, app: Flask) -> None:
        from mt5api import mcp_server

        monkeypatch.setattr(mcp_server, "_flask_app", lambda: app)
        self.mcp = mcp_server.build_mcp_server(chartctl_enabled=False, files_enabled=True)

    async def _call(self, tool: str, **args: Any) -> list:
        return _content_blocks(await self.mcp.call_tool(tool, args))

    async def ok(self, tool: str, **args: Any) -> dict:
        result = json.loads((await self._call(tool, **args))[0].text)
        if "status" in result and "body" in result:
            assert result["status"] < 400, result
            return result["body"]
        return result

    async def rejected(self, tool: str, **args: Any) -> tuple[int, dict]:
        """get_file and list_files raise on a non-200; the others return
        the status like every per-terminal tool."""
        try:
            result = json.loads((await self._call(tool, **args))[0].text)
        except ToolError as err:
            match = _TOOL_HTTP_ERROR.search(str(err))
            assert match, str(err)
            return int(match.group(1)), json.loads(match.group(2))
        assert result["status"] >= 400, result
        return result["status"], result["body"]

    async def refused(self, tool: str, **args: Any) -> str:
        with pytest.raises(ToolError) as excinfo:
            await self._call(tool, **args)
        return str(excinfo.value)


def _unified_server(transport, files: bool = True):
    from mcpunifier.client import TerminalClient
    from mcpunifier.config import Settings, Terminal
    from mcpunifier.mcp_server import build_mcp_server

    terminal = Terminal(
        broker=BROKER,
        account=ACCOUNT,
        instance="default",
        port=6545,
        mode="live",
        files=files,
    )
    settings = Settings(
        terminals={terminal.key: terminal},
        mt5_host="mt5",
        api_token="",
        request_timeout=5.0,
        listen_host="127.0.0.1",
        listen_port=6600,
        log_level="info",
        log_file="/dev/null",
    )
    return build_mcp_server(settings, TerminalClient(settings, transport=transport))


class _UnifiedHarness:
    def __init__(self, app: Flask) -> None:
        self.transport = _FlaskTransport(app)
        self.mcp = _unified_server(self.transport)

    async def _call(self, tool: str, **args: Any) -> list:
        routed = {"broker": BROKER, "account": ACCOUNT, **args}
        return _content_blocks(await self.mcp.call_tool(tool, routed))

    async def ok(self, tool: str, **args: Any) -> dict:
        result = json.loads((await self._call(tool, **args))[0].text)
        assert result.pop("terminal") == TERMINAL_KEY
        return result

    async def rejected(self, tool: str, **args: Any) -> tuple[int, dict]:
        with pytest.raises(ToolError) as excinfo:
            await self._call(tool, **args)
        match = _REJECTION.search(str(excinfo.value))
        assert match, str(excinfo.value)
        return int(match.group(1)), json.loads(match.group(2))

    async def refused(self, tool: str, **args: Any) -> str:
        with pytest.raises(ToolError) as excinfo:
            await self._call(tool, **args)
        return str(excinfo.value)


@pytest.fixture(params=["terminal", "unified"])
def server(request, monkeypatch, files_app):
    if request.param == "terminal":
        return _TerminalHarness(monkeypatch, files_app)
    return _UnifiedHarness(files_app)


def _run(coro):
    return asyncio.run(coro)


def test_put_get_list_delete_round_trip(server, file_trees):
    async def scenario():
        put = await server.ok("put_file", path="MQL5/Files/bot/state.json", content='{"a":1}')
        assert put["path"] == "MQL5/Files/bot/state.json"
        assert put["created"] is True

        got = await server.ok("get_file", path="MQL5/Files/bot/state.json")
        assert got["text"] == '{"a":1}'
        assert got["sha256"] == hashlib.sha256(b'{"a":1}').hexdigest()
        assert "content_base64" not in got

        listed = await server.ok("list_files", path="MQL5/Files/bot")
        assert [entry["name"] for entry in listed["entries"]] == ["state.json"]

        deleted = await server.ok("delete_file", path="MQL5/Files/bot", recursive=True)
        assert deleted["type"] == "dir"
        assert not (file_trees["terminal"] / "MQL5" / "Files" / "bot").exists()

    _run(scenario())


def test_binary_files_travel_as_base64(server, file_trees):
    data = bytes(range(256))

    async def scenario():
        await server.ok("put_file", path="MQL5/Libraries/x.dll", content_base64=_b64(data))
        got = await server.ok("get_file", path="MQL5/Libraries/x.dll")
        assert base64.b64decode(got["content_base64"]) == data
        assert "text" not in got

    _run(scenario())
    assert (file_trees["terminal"] / "MQL5" / "Libraries" / "x.dll").read_bytes() == data


def test_utf16_logs_come_back_as_text(server):
    async def scenario():
        got = await server.ok("get_file", path="logs/20261009.log")
        assert got["text"] == "journal"

    _run(scenario())


def test_extract_unpacks_a_zip_into_the_compile_tree(server, file_trees):
    archive = _zip({"Core/Base.mqh": b"base", "Signals.mqh": b"signals"})

    async def scenario():
        result = await server.ok(
            "put_file",
            path="Include/MyLib",
            content_base64=_b64(archive),
            extract=True,
            tree="compile",
        )
        assert result["count"] == 2

    _run(scenario())
    lib = file_trees["compile"] / "Include" / "MyLib"
    assert (lib / "Core" / "Base.mqh").read_bytes() == b"base"
    assert file_trees["changed"] == [True]


def test_credentials_are_refused(server):
    async def scenario():
        status, body = await server.rejected("get_file", path="mt5start.ini")
        assert (status, body["code"]) == (403, "PROTECTED")
        assert FILE_TREE_SECRET.decode() not in json.dumps(body)

    _run(scenario())


def test_listing_a_file_or_reading_a_directory_points_at_the_other_tool(server):
    async def scenario():
        assert "use get_file" in await server.refused("list_files", path="terminal64.exe")
        assert "use list_files" in await server.refused("get_file", path="MQL5")

    _run(scenario())


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("get_file", {"path": "../x"}, "'.' or '..'"),
        ("get_file", {"path": "MQL5/./x"}, "'.' or '..'"),
        ("get_file", {"path": "/etc/passwd"}, "relative"),
        ("list_files", {"tree": "host"}, "tree must be one of"),
        ("put_file", {"path": "", "content": "x"}, "path is empty"),
        ("delete_file", {"path": ""}, "path is empty"),
        ("put_file", {"path": "a.txt", "content": "x", "content_base64": "eA=="}, "not both"),
        ("put_file", {"path": "a.txt", "content_base64": "%%%"}, "not valid base64"),
    ],
)
def test_bad_arguments_are_refused_before_any_request(server, tool, args, message):
    async def scenario():
        assert message in await server.refused(tool, **args)

    _run(scenario())


def test_segments_are_percent_encoded(server, file_trees):
    """'#' and '%' would cut or corrupt the URL unless encoded; both are
    legal in a Windows file name."""
    async def scenario():
        await server.ok("put_file", path="MQL5/Files/a b#c%20.txt", content="x")

    _run(scenario())
    assert (file_trees["terminal"] / "MQL5" / "Files" / "a b#c%20.txt").read_bytes() == b"x"


def test_get_file_refuses_files_too_big_to_inline(server, monkeypatch, file_trees):
    from mcpunifier import mcp_server as unified
    from mt5api import mcp_server as per_terminal

    monkeypatch.setattr(per_terminal, "_FILE_MAX_INLINE_BYTES", 4)
    monkeypatch.setattr(unified, "FILES_MAX_INLINE_BYTES", 4)
    (file_trees["terminal"] / "MQL5" / "Files" / "big.bin").write_bytes(b"12345")

    async def scenario():
        assert "download it through the REST API" in await server.refused(
            "get_file", path="MQL5/Files/big.bin"
        )

    _run(scenario())


def test_unified_file_tool_on_a_terminal_without_the_routes_says_so():
    app = Flask("no_files")
    mcp = _unified_server(_FlaskTransport(app), files=False)

    async def scenario():
        with pytest.raises(ToolError, match="file API is not enabled on terminal"):
            await mcp.call_tool(
                "list_files", {"broker": BROKER, "account": ACCOUNT}
            )

    _run(scenario())


def test_unified_list_terminals_reports_files():
    app = Flask("no_files")
    mcp = _unified_server(_FlaskTransport(app), files=True)

    async def scenario():
        result = json.loads(_content_blocks(await mcp.call_tool("list_terminals", {}))[0].text)
        assert result["terminals"][0]["files"] is True

    _run(scenario())


def test_unified_endpoints_lists_the_file_routes_as_requiring_files(files_app):
    mcp = _unified_server(_FlaskTransport(Flask("x")))

    async def scenario():
        found = json.loads(_content_blocks(await mcp.call_tool("endpoints", {}))[0].text)
        tagged = {
            (entry["method"], entry["path"])
            for entry in found["endpoints"]
            if entry.get("requires") == "files"
        }
        # The catalog lists `<rel>`, not Flask's `<path:rel>`, like every
        # other route (see tests/integration/test_mcpunifier.py).
        served = {
            (method, str(rule).replace("<path:", "<"))
            for rule in files_app.url_map.iter_rules()
            if str(rule).startswith(("/files", "/compile/files"))
            for method in rule.methods - {"HEAD", "OPTIONS"}
        }
        assert tagged == served

    _run(scenario())


def test_terminal_server_registers_file_tools_only_when_enabled():
    from mt5api import mcp_server

    async def names(enabled):
        server = mcp_server.build_mcp_server(chartctl_enabled=False, files_enabled=enabled)
        return {tool.name for tool in await server.list_tools()}

    assert FILE_TOOLS <= asyncio.run(names(True))
    assert not FILE_TOOLS & asyncio.run(names(False))
