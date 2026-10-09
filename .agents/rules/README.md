# Project rules

Rules for agents and people changing mt5-httpapi. Each file covers one topic, each rule is meant to be checkable, and where a test, lint or script enforces a rule the rule names it. Start with [AGENTS.md](../../AGENTS.md) for the project overview.

Every rule starts with a bold ID: one prefix per file and a number counted from 1 in that file. Cite rules by ID (for example "TST3") in reviews, commits and code comments. IDs are never reused or renumbered: a removed rule's ID stays retired, and a new rule takes the next free number in its file.

| File | Prefix | Covers |
| --- | --- | --- |
| [architecture.md](architecture.md) | ARC | Process model, where each concern lives, what must stay in sync across components. |
| [python-code.md](python-code.md) | PY | Python style as practised here: guard clauses, named constants, logging, typed errors, exception handling. |
| [api-design.md](api-design.md) | API | REST surface: snake_case keys and the legacy camelCase twins, error shape, status codes, feature gating, body caps. |
| [mcp.md](mcp.md) | MCP | The two MCP servers: tool parity, the route catalog, docstrings as the agent contract, errors. |
| [testing.md](testing.md) | TST | Which test layer for what, regression tests, live suite safety and cleanup. |
| [windows-vm.md](windows-vm.md) | WIN | Code that runs inside the Windows VM: ASCII only, boot flow, the shared folder, locks. |
| [security.md](security.md) | SEC | Credentials, auth tokens, file API and compile protections, live-account safety, supply-chain review. |
| [docs-and-releases.md](docs-and-releases.md) | DOC | Docs kept in step with code, prose style, CHANGELOG, versions, tags, CI publishing. |
| [chat.md](chat.md) | CHAT | How an agent signs off its chat replies to the maintainer. |

When a rule here and the code disagree, the code and its tests win; fix the rule in the same change.
