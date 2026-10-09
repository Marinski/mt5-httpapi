"""Clients for a running mt5-httpapi stack: one terminal's REST API, and the
two MCP endpoints (that terminal's own /mcp/ and the unified /mcp/ at the
server root).

The bearer token goes into the session headers once and is never part of a
message or assertion text.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import requests

REST_TIMEOUT_SECONDS = 60
# Uploads refresh the Navigator through AutoIt, and WebRequest applies can
# wait minutes for the host's GUI lock.
MCP_TIMEOUT_SECONDS = 320
MCP_PATH = "/mcp/"


class LiveAPIError(AssertionError):
    """A call to the live stack answered something other than success."""


@dataclass(frozen=True)
class Target:
    """The running stack and the terminal the suite acts on."""

    url: str
    token: str
    broker: str
    account: str
    instance: str

    @property
    def terminal_path(self) -> str:
        return f"/{self.broker}/{self.account}/{self.instance}"

    @property
    def terminal_key(self) -> str:
        return f"{self.broker}/{self.account}/{self.instance}"


def _session(token: str) -> requests.Session:
    session = requests.Session()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


class RestClient:
    """REST calls against the target terminal's base URL."""

    def __init__(self, target: Target) -> None:
        self.base = target.url.rstrip("/") + target.terminal_path
        self.session = _session(target.token)

    def request(
        self,
        method: str,
        path: str,
        expect: int | tuple[int, ...] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", REST_TIMEOUT_SECONDS)
        resp = self.session.request(method, self.base + path, **kwargs)
        wanted = (expect,) if isinstance(expect, int) else expect
        ok = resp.status_code in wanted if wanted else 200 <= resp.status_code < 300
        if not ok:
            raise LiveAPIError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs).json()


class McpClient:
    """JSON-RPC tool calls against one streamable-HTTP MCP endpoint."""

    def __init__(self, url: str, token: str, routing: dict[str, str] | None = None) -> None:
        self.url = url
        self.routing = routing or {}
        self.session = _session(token)
        self.session.headers["Accept"] = "application/json, text/event-stream"

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.post(
            self.url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            timeout=MCP_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise LiveAPIError(f"MCP {method} -> {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        if "error" in body:
            raise LiveAPIError(f"MCP {method} error: {body['error']}")
        return body["result"]

    def tool_names(self) -> set[str]:
        return {tool["name"] for tool in self.rpc("tools/list")["tools"]}

    def call(self, tool: str, routed: bool = True, **args: Any) -> list[dict[str, Any]]:
        """Call a tool and return its content blocks; raise on a tool error."""
        arguments = {**(self.routing if routed else {}), **args}
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise LiveAPIError(f"{tool} failed: {result['content'][0].get('text', '')[:800]}")
        return result["content"]

    def call_json(self, tool: str, routed: bool = True, **args: Any) -> dict[str, Any]:
        return json.loads(self.call(tool, routed=routed, **args)[0]["text"])

    def call_error(self, tool: str, routed: bool = True, **args: Any) -> str:
        """Call a tool that must fail; return its error text."""
        arguments = {**(self.routing if routed else {}), **args}
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        if not result.get("isError"):
            raise LiveAPIError(f"{tool} succeeded but was expected to fail: {result}")
        return result["content"][0].get("text", "")


def terminal_mcp(target: Target) -> McpClient:
    """The target terminal's own /mcp/ endpoint (no routing arguments)."""
    url = target.url.rstrip("/") + target.terminal_path + MCP_PATH
    return McpClient(url, target.token)


def unified_mcp(target: Target) -> McpClient:
    """The unified /mcp/ endpoint, routed to the target terminal."""
    url = target.url.rstrip("/") + MCP_PATH
    routing = {
        "broker": target.broker,
        "account": target.account,
        "instance": target.instance,
    }
    return McpClient(url, target.token, routing)


def unwrap_terminal_result(result: dict[str, Any]) -> dict[str, Any]:
    """The per-terminal server returns {"status", "body"}; fail on non-2xx."""
    status = result.get("status", 0)
    if not 200 <= status < 300:
        raise LiveAPIError(f"terminal MCP answered {status}: {result.get('body')}")
    return result["body"]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
