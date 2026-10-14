# Repository Guidelines

## Project Structure

This Python 3.13+ project implements a deterministic A2A agent and an MCP server. Agent code is in `agente/`; the server is in `servidor-mcp/`. Shared policy and fixtures are in `dados/`, protocol examples in `exemplos/wire/`, implementation prompts in `docs/`, and the conformance client in `validador/`. Keep all automated tests in the repository-root `test/` folder. `pyproject.toml` and `uv.lock` define the environment.

## Setup, Run, and Verify

Install the locked dependencies with `uv sync --frozen`. Set `REQUEST_STATE_SECRET` to at least 32 random bytes before starting the MCP server; for local work, generate a hex secret with `python3 -c "import secrets; print(secrets.token_hex(32))"`.

```sh
uv run --frozen python servidor-mcp/servidor.py  # MCP on port 7301
uv run --frozen python -m agente.main            # A2A on port 7300
uv run --frozen python -m unittest discover -s test -v
uv run --frozen python test/verificar_http.py
uv run --frozen python test/verificar_agente_http.py
```

The HTTP verification scripts start and stop their own processes; keep their configured ports free. For manual end-to-end validation, start both services and run `uv run --frozen python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301`. Restart services before repeating validation because reservations are held in memory.

## Style and Architecture

Use four-space indentation, `snake_case` for functions and variables, and `CapWords` for classes. Preserve Portuguese domain terms and wire-contract field names. The agent coordinates A2A Tasks and forwards reservation decisions to MCP; business rules belong in the MCP server. Keep `requestState` opaque and out of public A2A responses. Never commit secrets.

## Testing and Code Tasks

Use fail-first TDD for code changes: add a test for expected behavior, run it and confirm it fails for the intended reason, implement the smallest change, then rerun it. Tests must cover meaningful business behavior or challenge requirements, such as reservation constraints, conflict and retry behavior, or protocol responses. Avoid tests that only mirror implementation details. Use `unittest`; no coverage threshold is configured.

## Commits and Pull Requests

Recent commits use short imperative subjects with prefixes such as `chore:` and `delete:`. Keep each commit focused. A pull request should describe behavior and affected A2A/MCP flows, list verification commands and results, and include wire examples when useful. Link the related requirement or issue when available.
