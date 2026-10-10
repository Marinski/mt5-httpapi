"""Keep the per-terminal and unified typed MCP signatures aligned."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKET_DATA_TOOLS = ("get_rates", "get_ticks", "get_rates_ta")
UNIFIED_ROUTING_PARAMETERS = {"broker", "account", "instance"}
# Unified-only: routing discovery has no per-terminal counterpart.
UNIFIED_ONLY_TOOLS = {"list_terminals"}
# Tools that act on no single terminal, so they take no routing parameters.
UNROUTED_TOOLS = {"endpoints", "list_terminals"}


def _is_tool(node):
    return isinstance(node, ast.AsyncFunctionDef) and any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "tool"
        for decorator in node.decorator_list
    )


def _parameter_specs(node):
    """[(name, annotation, default)] with annotation and default as source."""
    arguments = node.args.args
    defaults = [None] * (len(arguments) - len(node.args.defaults)) + list(node.args.defaults)
    return [
        (
            argument.arg,
            ast.unparse(argument.annotation) if argument.annotation else None,
            ast.unparse(default) if default is not None else None,
        )
        for argument, default in zip(arguments, defaults, strict=True)
    ]


def _registered_tools(path):
    """{tool name: [(parameter, annotation, default)]} for every tool."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name: _parameter_specs(node) for node in ast.walk(tree) if _is_tool(node)}


def test_every_tool_exists_on_both_servers_with_the_same_parameters():
    """A tool on one server and not the other, or with a different argument
    list, type or default, means an agent's call behaves differently on the
    two endpoints. The unified copy adds broker/account first and instance
    last."""
    terminal = _registered_tools(REPO_ROOT / "mt5api" / "mcp_server.py")
    unified = _registered_tools(REPO_ROOT / "mcpunifier" / "mcp_server.py")

    assert set(unified) - UNIFIED_ONLY_TOOLS == set(terminal)
    for tool, parameters in terminal.items():
        routed = unified[tool]
        if tool in UNROUTED_TOOLS:
            assert routed == parameters, tool
            continue
        assert [name for name, _, _ in routed[:2]] == ["broker", "account"], tool
        assert routed[-1][0] == "instance", tool
        assert routed[2:-1] == parameters, tool


def _tool_parameters(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: [argument.arg for argument in node.args.args]
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in MARKET_DATA_TOOLS
    }


def test_market_data_tool_parameters_match_between_mcp_servers():
    terminal = _tool_parameters(REPO_ROOT / "mt5api" / "mcp_server.py")
    unified = _tool_parameters(REPO_ROOT / "mcpunifier" / "mcp_server.py")

    assert set(terminal) == set(MARKET_DATA_TOOLS)
    assert set(unified) == set(MARKET_DATA_TOOLS)
    for tool in MARKET_DATA_TOOLS:
        unified_parameters = [
            parameter
            for parameter in unified[tool]
            if parameter not in UNIFIED_ROUTING_PARAMETERS
        ]
        assert unified_parameters == terminal[tool]


def test_tick_and_ta_tools_expose_explicit_range_parameters():
    terminal = _tool_parameters(REPO_ROOT / "mt5api" / "mcp_server.py")

    assert {"from_", "to"} <= set(terminal["get_ticks"])
    assert {"from_", "to"} <= set(terminal["get_rates_ta"])
