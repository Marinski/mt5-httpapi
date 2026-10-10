# Testing rules

Prefix: TST.

How to run each layer: [AGENTS.md](../../AGENTS.md#build-run-test) and [docs/operations.md](../../docs/operations.md#testing-a-running-stack).

## What every change needs

- **TST1:** Every bug fix comes with a regression test that fails without the fix and passes with it, at the lowest layer that can reproduce the bug. Name the test after the behaviour it pins, the way the suite does (`test_a_down_terminal_fails_alone`, `test_compile_token_is_refused_on_every_other_route`).
- **TST2:** Every new route, tool, config setting or script behaviour gets tests in the same change.
- **TST3:** `make test` passes before a change is called done. It runs `verify-binaries`, `test-unit`, `test-integration` and `test-go`, the same gate CI runs. If you changed `.sh` or `.ps1` files, `make lint` passes too.
- **TST4:** The unit coverage floor on `mt5api` (`--cov-fail-under=62` in `Dockerfile.test`) is a regression gate. Raise it when coverage grows; never lower it to make a build pass.

## Which layer

- **TST5:** `tests/` (unit and contract, `make test-unit`): handler contracts through the Flask test client, parsers, builders, config generation, script logic, both MCP servers' tool behaviour, parity. Offline, no Docker socket, no network. The `MetaTrader5` SDK is a stub installed by `tests/conftest.py`; script SDK results through `RecordedMT5`, `patch_handler` and `api_client` instead of adding real SDK calls.
- **TST6:** Test fixtures use obviously fake values (symbol `TESTUSD`, ticket `12345`, made-up logins and tokens), never a real broker name, account number, server or price. `tests/test_live_api_contract.py` shows the pattern.
- **TST7:** `tests/integration/` (`make test-integration`, testcontainers on the host): anything that needs real containers. nginx accepting and serving the generated config, the unifier image beside a stub terminal, the vm-watchdog recovery through real `docker compose`, the wickworks sidecar lifecycle. Use it when the bug lives between components or in Docker behaviour a unit test would have to fake.
- **TST8:** `clients/go/` (`make test-go`): every change to the Go client keeps `go test -race ./...` green, and a REST change that the Go client models updates the client and its tests.
- **TST9:** `tests/live/` (`make test-live`): checks a deployed stack. It is not part of `make test` or CI. Use it to confirm behaviour that only a real terminal, MetaEditor or nginx in front of the VM shows, and add a live test for such behaviour alongside the offline one.
- **TST10:** When a flow can only be fully exercised live, mirror it offline with the SDK scripted in `tests/test_live_api_contract.py` so CI still covers the route, handler and client path.

## Live suite safety

- **TST11:** Never run `make test-live` without the user asking for it in that turn. It places, modifies and closes real orders (`test_market_order`, `test_limit_order`, `test_position_management`, `test_history`).
- **TST12:** A live test that changes state (orders, positions, deployments, uploads, file writes, the WebRequest allowlist) depends on the `state_changes_allowed` fixture in `tests/live/conftest.py`, which skips on a non-demo account unless `MT5_LIVE_ALLOW_REAL=1`. Do not set that variable yourself.
- **TST13:** Every artifact a live test creates is named with `ARTIFACT_PREFIX` (`livetest-`), and the suite removes them before and after the run (`purge_artifacts`, `purge_files`). Every order carries `MT5_LIVE_MAGIC`, and the suite only ever closes orders and positions with that magic. A new live test that creates anything follows both rules and cleans up even when it fails.
- **TST14:** Changing the WebRequest allowlist drives the terminal GUI, so those tests run only with `MT5_LIVE_WEBREQUEST=1`.
- **TST15:** Live targets and tokens come from `MT5_LIVE_*` variables or the gitignored `tests/live/.env`. Never write a real URL, token, broker or account into a test file, a fixture or `tests/live/.env.example`.
