# Security rules

Prefix: SEC.

This API trades real accounts and runs inside a VM that holds broker credentials. The repository is public.

## Secrets and credentials

- **SEC1:** Never commit, print, quote or paste into docs, tests, commit messages or issues: anything from `config/config.yaml`, `vms.yaml`, any `.env` (including `tests/live/.env`), broker logins, servers or passwords, API tokens, Tailscale or Cloudflare keys, account numbers, or hostnames and IPs of real machines. These files are gitignored; keep them that way. Use `config/config.yaml.example`, `vms.yaml.example` and `tests/live/.env.example` with placeholder values for anything that must be shown.
- **SEC2:** The file API never lists or serves the broker credentials: `mt5start.ini` and `Config/accounts.dat` are hidden in the terminal tree (`_TERMINAL_HIDDEN` in `mt5api/fileapi/__init__.py`, `tests/test_files_api.py::test_credentials_are_refused`, `tests/test_mcp_files_tools.py::test_credentials_are_refused`). No other route may expose them either.
- **SEC3:** Logs never carry tokens, passwords or trading request bodies (PY15). The unifier redacts secret-looking keys in its JSON logs (`mcpunifier/logging.py`); do not weaken that pattern.

## Authentication

- **SEC4:** One bearer token, `api_token` in `config.yaml`, guards every REST route and both MCP endpoints. An empty token disables auth everywhere, consistently. The per-terminal `/mcp` mount bypasses Flask, so `_mcp_auth_gate` in `mt5api/main.py` repeats the check; any new non-Flask mount needs the same gate.
- **SEC5:** `compile_api_token` is a second, compile-only credential: accepted on `POST /compile` and nowhere else, so a caller that only compiles cannot trade or restart a terminal. It is not accepted on `/compile/files/...`. Enforced by `tests/test_compile.py::test_compile_token_is_refused_on_every_other_route`. Never widen it to another route.
- **SEC6:** The unifier requires the bearer on everything except `/health` (`BearerAuthMiddleware` in `mcpunifier/server.py`) and sends the API token to terminals itself.

## File API

- **SEC7:** Every caller path goes through `Tree.resolve` in `mt5api/fileapi/tree.py` before any operation: no absolute or drive paths, no `..`, no Windows device names (`con`, `nul`, `com1`, ...), no characters Windows refuses, no symlinks or junctions leading out of the tree, case-insensitive comparison. The API's own staging files (`.mt5api-` prefix) are never addressable. Covered by `test_paths_that_could_leave_the_tree_are_refused`, `test_a_symlink_out_of_the_tree_is_refused` and `test_paths_are_checked_before_any_write`.
- **SEC8:** The terminal executables (`terminal64.exe`, `metaeditor64.exe`, `metatester64.exe`) and Chart Deployments' own files (`MQL5/Experts/Uploaded`, `MQL5/Files/chartctl`, `chartctl`) are read-only through the file API. A directory holding protected paths cannot be deleted.
- **SEC9:** `?extract` checks every archive entry before writing anything, refuses corrupt, encrypted or symlink entries and duplicate names, counts the inflated size rather than the declared one, and is capped by `files.max_extract_bytes` and `files.max_extract_files` (`tests/test_files_api.py` extract tests). Any new archive or upload path keeps those guarantees.
- **SEC10:** The file API is off unless `files.enabled: true`. Do not turn it on by default.

## Compile sandbox

- **SEC11:** `POST /compile` takes source text only. The filename is reduced to a bare stem, MetaEditor's `/log:` and `/inc:` are computed by the server, all work happens in a per-request temp directory removed on every exit, and `#include`, `#resource` and `#property icon` paths are checked to stay inside the source directory or the server's include tree before MetaEditor runs (`_check_source_paths` in `mt5api/handlers/compile.py`; `test_source_cannot_name_a_file_outside_the_sandbox`, `test_caller_controls_neither_the_log_nor_the_include_argument`, `test_temp_directory_is_removed_after_success_and_failure`). Unexpected errors answer without class, message or paths (PY11). Read the threat model at the top of `compile.py` before changing it.

## Supply chain

- **SEC12:** Every tracked executable (PE, ELF, Mach-O, `.ex5`) is declared in `assets/binaries.lock.json` with its path, sha256, size, product, version, vendor, upstream source URL, signature state and a note on how it was verified. `make verify-binaries` (first step of `make test`, so CI) fails on an undeclared binary, changed bytes or a degraded signature (`scripts/verify_binaries.py`, `tests/test_verify_binaries.py`). Add a binary only after checking it against the vendor's published hash or archive yourself; if it cannot be verified, say so in its `note`.
- **SEC13:** Review an outside contribution's build, CI and runtime-plumbing changes before running any of it: `Makefile`, `Dockerfile.*`, `.github/workflows/`, `run.sh`, `scripts/*.bat`, `scripts/*.ps1`, `scripts/*.sh`, `docker-compose.yml.j2`, `requirements-*.txt`, `.agents/plugins/` and any binary. `make test`, `make lint` and `make up` execute that code with Docker access. Read the diff first; do not run a contributor's branch to find out what it does.
- **SEC14:** Pin new dependencies at least as tightly as their neighbours: base images of `Dockerfile.test`, `Dockerfile.mcpunifier` and `Dockerfile.watchdog`, the wickworks image and the Go test image are pinned by digest; third-party GitHub Actions by commit SHA; `requirements-test.txt` by exact version and `requirements-watchdog.txt` by hash; `mcp==1.28.0` exactly in both MCP consumers. `requirements-api.txt` and `requirements-mcpunifier.txt` are otherwise unpinned; do not loosen anything that is pinned, and say in the CHANGELOG what changed when you bump one.
- **SEC15:** Vendored third-party code keeps its license and is listed in `THIRD_PARTY.md`.

## Live accounts

- **SEC16:** Order, position, terminal (`/terminal/restart`, `/terminal/shutdown`, `/terminal/init`), deployment, WebRequest and file-write calls change a real broker account or terminal. Never make them against a running stack, and never run `make test-live`, unless the user asked for that specific action in the current turn. Prefer a demo account; see TST11 to TST13.
