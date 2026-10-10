# Architecture rules

Prefix: ARC.

## Process model

- **ARC1:** One `mt5api` process serves exactly one terminal (broker, account, instance). Never make one process talk to a second terminal; the SDK is single-connection per process.
- **ARC2:** Every handler that calls the `MetaTrader5` SDK is wrapped in `@with_mt5` or runs inside `session()` from `mt5api/mt5client.py`, and calls the SDK through `m(...)`, so it holds the process-wide lock, respects `MT5_MAX_QUEUE_DEPTH` and gets the timeout and wedge handling.
- **ARC3:** A handler that touches no SDK (chartctl, files, compile) does not take the MT5 lock; `mt5api/chartctl/__init__.py` documents why.
- **ARC4:** `/ping` stays lock-free so health checks answer while the terminal is busy.
- **ARC5:** The per-terminal MCP server calls the Flask app in process (`_call_wsgi` in `mt5api/mcp_server.py`). Do not add a second code path for a tool; add or change the REST handler and let the tool proxy to it.
- **ARC6:** The unified MCP server (`mcpunifier/`) is a separate image and process. It does not import `mt5api`; it reaches terminals over HTTP only and reads `config/config.yaml` itself (`mcpunifier/config.py`).

## Single sources of truth

- **ARC7:** `config/config.yaml` is the only user config. A new setting is documented in `config/config.yaml.example` and [docs/installation-and-configuration.md](../../docs/installation-and-configuration.md) and read in `mt5api/config.py`, with the upper-case env var of the same name taking precedence where the setting family already works that way (`_compile_setting`, `_positive_int_setting`).
- **ARC8:** `scripts/config_helper.py` generates the nginx config, the compose file (from `docker-compose.yml.j2` when `vms.yaml` exists) and each terminal's `mt5start.ini`. No other script writes those outputs.
- **ARC9:** `config_helper.py` cannot import `mt5api` because it runs on the host, so logic it shares with `mt5api/config.py` is duplicated and must stay identical: `_feature_block` and `feature_block`, the chartctl rule in `write_ini` and `chartctl_enabled`. `mcpunifier/config.py` applies the same chartctl and files rules. Change every copy together; `tests/test_config_generation.py` and `tests/test_mcpunifier_config.py` cover them.
- **ARC10:** The terminal list `start.bat` launches (`config_helper.py terminals`) and the ports `scripts/check_health.py` probes use the same VM group filter. Keep them in agreement.

## Feature gates

- **ARC11:** An optional feature is off by default and opt-in through a `config.yaml` block (`chartctl`, `files`). `true` is shorthand for `{enabled: true}`, a per-terminal `false` opts one terminal out, and any other non-mapping value counts as off in the API.
- **ARC12:** A gated feature's routes are registered only when enabled (`if CHARTCTL_ENABLED` and `if FILES_ENABLED` in `mt5api/server.py`). Its route table lives in one function (`register_chartctl_routes`, `register_files_routes`) that the server, the tests and the MCP catalog test all use.
- **ARC13:** Chart Deployments are live-mode only; the file API works in any mode.

## Deployment path

- **ARC14:** Code reaches the VM only through `run.sh` copying into `data/shared/`. A new file the VM needs is added to the copy list in `run.sh` (scripts), lives under `mt5api/` (copied whole) or lives under `assets/` (mounted read-only at `/shared/assets`).
- **ARC15:** A new Python dependency of the in-VM API goes in `requirements-api.txt` and in the `pip install` line of `scripts/start.bat`; `tests/test_requirements_sync.py` checks they match and that both MCP consumers pin a compatible `mcp` SDK.
- **ARC16:** A route that can legitimately run longer than nginx's 60 second default goes in `LONG_RUNNING_ROUTES` or gets its own timeout in `config_helper.py`; `tests/test_config_generation.py::test_only_the_long_running_routes_wait_longer_than_nginx_default` pins the list. Long-running unifier calls stay under nginx's 300 seconds on `/mcp/` (`WEBREQUEST_TIMEOUT_SECONDS`, `FILES_TIMEOUT_SECONDS` in `mcpunifier/constants.py`).
- **ARC17:** nginx reaches upstreams through a variable plus a resolver, so one absent VM container does not stop nginx from starting (`test_nginx_routes_never_use_a_literal_upstream_host`, `tests/integration/test_nginx_routing.py`).
- **ARC18:** A broker may not be named `mcp`; `config_helper.py nginx_conf` refuses it because `/mcp/` belongs to the unifier.

## Chart Deployments protocol

- **ARC19:** The API and the loader EA talk only through files under `MQL5\Files\chartctl\`, as specified in [docs/chart-control-protocol.md](../../docs/chart-control-protocol.md). A protocol change updates that doc, `assets/experts/MT5ChartLoader.mq5` or `assets/experts/include/ChartControl.mqh`, and `mt5api/chartctl/` together, and bumps `CHARTCTL_VERSION` in `ChartControl.mqh` when loader behaviour changes.
