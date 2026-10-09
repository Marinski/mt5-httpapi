# MCP rules

Prefix: MCP.

Two MCP servers expose the same tools: the per-terminal one in `mt5api/mcp_server.py` (mounted at `/<broker>/<account>[/<instance>]/mcp`) and the unified one in `mcpunifier/mcp_server.py` (at `/mcp/`). User docs: [docs/mcp-and-agents.md](../../docs/mcp-and-agents.md).

## Parity

- **MCP1:** Every tool exists on both servers with the same name, parameter names, annotations and defaults. The unified copy adds `broker` and `account` as its first two parameters and `instance` as its last; `endpoints` takes no routing parameters, and `list_terminals` exists only on the unified server. Enforced by `tests/test_mcp_tool_parity.py`, which parses both files.
- **MCP2:** A tool is added, renamed or changed on both servers in the same change. If a tool is truly server-specific, add it to `UNIFIED_ONLY_TOOLS` or `UNROUTED_TOOLS` in the parity test with a comment saying why.
- **MCP3:** Tools are thin wrappers that map typed arguments to one REST call. Business logic stays in the Flask handler (ARC5); the unified tool sends the same method, path, query and body over HTTP that the per-terminal tool sends in process.

## Route catalog

- **MCP4:** The unifier cannot read Flask's `url_map`, so `_ROUTE_CATALOG`, `_CHARTCTL_ROUTE_CATALOG` and `_FILES_ROUTE_CATALOG` in `mcpunifier/mcp_server.py` are maintained by hand. They must list exactly the routes Flask registers with chartctl and files on, with placeholders written without converters (`<ticket>`, not `<int:ticket>`). Enforced by `tests/integration/test_mcpunifier.py::test_the_endpoints_tool_returns_exactly_the_real_flask_routes` and checked against a running terminal by `tests/live/test_mcp.py::test_every_catalogued_route_exists_on_the_terminal`.
- **MCP5:** Routes that exist only with a feature on go in that feature's catalog so `endpoints` tags them (`test_the_endpoints_tool_marks_exactly_the_chartctl_routes`, `..._file_routes`).

## Registration

- **MCP6:** On the per-terminal server, Chart Deployments and file API tools are registered only when the feature is enabled for that terminal (`test_terminal_server_registers_chartctl_tools_only_when_enabled`, `test_terminal_server_registers_file_tools_only_when_enabled`). The unified server always registers them and turns the terminal's 404 into `ChartctlDisabled` or `FilesDisabled`, which say how to enable the feature.
- **MCP7:** `list_terminals` reports per terminal whether `chartctl` and `files` are on, using the same rule as the API (ARC9).

## Docstrings are the contract

- **MCP8:** FastMCP sends each tool's docstring to the agent as its description, so the docstring is the interface. It states what the tool does, every parameter's meaning, allowed values and units, defaults, and what comes back. Keep the two servers' docstrings in agreement apart from the routing parameters.
- **MCP9:** A tool that moves money on the account says so in its docstring: irreversible on a live account, call only when the user explicitly asked for that action. Today `create_order`, `close_position`, `cancel_order` and the generic `request` tool carry it, and the server instructions repeat it for every trading tool. A new trading tool carries it too.
- **MCP10:** The unified server never defaults the terminal: `broker` and `account` are required, and every unified response carries `terminal` with the key that answered.
- **MCP11:** Tool lists in the module docstring of `mt5api/mcp_server.py`, in [docs/mcp-and-agents.md](../../docs/mcp-and-agents.md) and in the agent skill are updated with the tool (DOC1).

## Results and errors

- **MCP12:** Per-terminal JSON tools return `{"status": <http status>, "body": <parsed JSON or {"raw": text}>}` for every status, so the agent sees MT5 retcodes and validation messages as data. Do not raise for a non-2xx answer in those tools.
- **MCP13:** Unified JSON tools return the terminal's JSON object plus `terminal` on 2xx (a non-object body is wrapped as `{"result": ...}`) and raise on anything else: `TerminalRejected` with the status and upstream body, `TerminalUnreachable` when the terminal does not answer, `UnknownTerminal` with the configured list. One down terminal fails only its own call (`test_a_down_terminal_fails_alone`).
- **MCP14:** Bad arguments are refused before any request: `ToolError` on the per-terminal server, `ToolArgumentError` on the unified one (`test_bad_arguments_are_refused_before_any_request` in both MCP tool test files). Binary and file tools, which cannot hand back a status payload, raise `ToolError` with the HTTP status and detail on failure.
- **MCP15:** Uploads take file content as base64 (wrapped base64 accepted; `upload_set` and `put_file` also take text) and send the same multipart form the REST route reads. `screenshot_chart` returns MCP image content and the unified server refuses a non-PNG answer.
- **MCP16:** Long calls get explicit unified timeouts below nginx's 300 seconds (ARC16), and the per-terminal proxy logs method and route only, never body or query (PY15).
