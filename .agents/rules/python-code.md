# Python code rules

Prefix: PY.

There is no Python linter or formatter in CI (`make lint` covers shell and PowerShell only), so these rules are kept by review. Match the style of the module you are in.

## Structure

- **PY1:** Use guard clauses: check, return the error early, keep the happy path unindented. Handlers follow the shape in `mt5api/handlers/orders.py`: `if not ensure_initialized(): return ... 503`, then body validation, then the work.
- **PY2:** No magic numbers or strings in logic. Name them as module constants with a comment saying why the value is what it is (`WSGI_THREADS`, `MCP_SESSION_START_TIMEOUT` in `mt5api/main.py`, `MAX_REQUEST_BODY_BYTES` in `mt5api/config.py`). In `mcpunifier/`, shared values live in `mcpunifier/constants.py`.
- **PY3:** Configurable values are read once in `mt5api/config.py` (or `mcpunifier/config.py`) and imported as constants. Handlers do not read `os.environ` or `config.yaml` themselves.
- **PY4:** Invalid optional settings fall back to the default with a warning instead of stopping the API (`_positive_int_setting`, `_compile_timeout`, `feature_block` in `mt5api/config.py`). Trading must not stop because an optional block is malformed. The unifier is stricter and fails at startup with `ConfigError`.
- **PY5:** Docstrings and comments explain why, including the incident or constraint behind a choice (see `_refuse_oversized_body` in `mt5api/server.py`, the threat model at the top of `mt5api/handlers/compile.py`). Do not write comments that restate the code.
- **PY6:** New modules use type hints; `mcpunifier/` is fully typed and newer `mt5api` modules (`jsonkeys.py`, `fileapi/`) use `from __future__ import annotations`.

## Errors

- **PY7:** Raise typed exceptions for failures a caller must tell apart, and map them to HTTP in one place. Examples: `mt5api/fileapi/errors.py` mapped by `_ERROR_STATUS` in `mt5api/handlers/files.py`; `QueueFull`, `MT5Wedged`, `MT5Timeout` mapped by `with_mt5` in `mt5api/mt5client.py`; `mcpunifier/errors.py` (`UnknownTerminal`, `TerminalUnreachable`, `TerminalRejected`, `ToolArgumentError`, ...).
- **PY8:** Operation modules stay free of HTTP. `mt5api/fileapi/ops.py` raises `FileApiError` subclasses and never builds a response.
- **PY9:** No bare `except:`. No silent `except Exception: pass` in new code. A broad catch is allowed only where failure of an optional step must not break the main one, and then it logs (`log.exception` or `log.warning`) and carries a `# noqa: BLE001 - <reason>` comment, as in `mt5api/main.py` and `mt5api/handlers/compile.py`. A few older spots swallow silently; do not copy them.
- **PY10:** Chain re-raised exceptions with `from err` so the cause survives (`mcpunifier/client.py`).
- **PY11:** Never put an exception's class, message or a server path in a response where a caller could provoke it on purpose; log the traceback server-side instead (`tests/test_compile.py::test_an_unexpected_error_does_not_leak_its_class_message_or_paths`).

## Logging

- **PY12:** In `mt5api/`, log through `from mt5api.logger import log` (the `mt5api` logger) or a child of it (`logging.getLogger(__name__)` inside the package). It writes to stdout, which `api_runner.bat` captures per terminal, and to the shared `logs/full.log` behind a mkdir lock. Do not add handlers to the root logger; `log.propagate = False` is deliberate.
- **PY13:** `mt5api` log calls use %-style arguments, not f-strings, and start request-scoped lines with the request id (`g.req_id`) the way `mt5api/server.py` and `mt5api/mt5client.py` do.
- **PY14:** In `mcpunifier/`, log through `logging.getLogger(__name__)` with structured fields in `extra=` (for example `extra={"terminal": terminal.key, "reason": ...}`). `mcpunifier/logging.py` emits JSON, adds the request scope (`request_id`, `terminal`) through `with_scope`, and redacts keys matching password, token, secret, authorization, cookie and api key.
- **PY15:** Never log request bodies or query strings of trading calls, tokens or credentials. The MCP proxy logs the method and route only (`_call` in `mt5api/mcp_server.py`).
- **PY16:** No `print()` in `mt5api/` or `mcpunifier/`. Scripts under `scripts/` that act as CLIs may print.
