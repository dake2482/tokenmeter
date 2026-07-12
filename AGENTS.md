# TokenMeter Repository Guidelines

## Shared Policy

Also follow `/Users/dake/Documents/AGENT_POLICY.md`. This file is the TokenMeter-specific contract and adds only repository routing, validation, and the historical local-host audit compatibility workflow.

## Repository Purpose

TokenMeter is a self-hosted Python application that collects token-usage metadata from Hermes, OpenClaw, Codex, ZCode, WorkBuddy, and Claude Code. It stores normalized usage in SQLite, supports uploads from multiple machines, and exposes a Web dashboard. It must not collect message bodies, prompts, responses, credentials, or auth material.

## Project Structure & Module Organization

- `src/tokenmeter/` contains collectors, normalized records, SQLite storage, summaries, the HTTP server/dashboard, and the CLI entry point.
- `tests/test_tokenmeter.py` contains the deterministic standard-library unit and integration tests.
- `scripts/install.sh` installs either the central server or a periodic uploader.
- `assets/` contains dashboard icons and other static assets.
- `docs/` contains user and operational documentation; `docs/project/` contains the project-control contract.

Keep runtime databases, logs, generated usage data, credentials, machine-local environment files, and build/cache output out of Git.

## Canonical Validation Commands

Run these from the repository root:

- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m py_compile src/tokenmeter/*.py`
- `sh -n scripts/install.sh`

Run all three before handing off a source or installer change. Tests must not require external services unless the requirement explicitly documents that dependency.

## Coding Style & Naming Conventions

Follow the existing standard-library Python style, use descriptive `snake_case` names, preserve type annotations, and keep collectors isolated by source. Normalize external records before storage and avoid reading message content. Use `kebab-case` for shell scripts and static assets.

## Testing Guidelines

Add or update deterministic tests for every behavioral change. Use temporary files and databases, cover malformed or missing source data, and ensure data-source changes cannot ingest prompts, responses, secrets, or other message content.

## Project Control Files

- `docs/project/project.yaml` records the current milestone, health, next action, and validation commands.
- `docs/project/requirements/*.md` is the source of truth for requirement status, task ownership, branch/worktree, acceptance criteria, handoff, and evidence.
- `docs/project/DECISIONS.md` is append-only and records durable implementation or governance decisions.
- TokenMeter requirement IDs use the `TOKENMETER-NNN` prefix. Cross-project status rules and closeout gates come from the shared policy.

## Commit & Pull Request Guidelines

Use clear imperative commit messages. Pull requests should include a short summary, validation commands and results, linked requirement or issue, and screenshots for dashboard changes. Preserve unrelated user changes and stage only the intended files.

## Security & Configuration Tips

Never commit secrets, API keys, bearer tokens, certificates, databases, logs, usage exports, or machine-specific `.env` files. Commit safe examples instead and document required variable names without real values.

## Compatibility: Local Host Service Health Audit

This historical Server-repository workflow is separate from TokenMeter product validation. Keep it available for local host operations.

When the user says `检查服务器状态`, run the checks in this section and return a complete analysis report. Include an overall health summary, per-service status, profile/model inventory, detected issues, supporting command evidence, and recommended next actions. Before changing local service behavior, verify live state with read-only checks. For Hermes, inspect all profiles:

- `launchctl list | rg -i 'ai.hermes.gateway'`
- `lsof -nP -iTCP:8642-8646 -sTCP:LISTEN`
- `curl -sS --max-time 2 http://127.0.0.1:<port>/v1/models`

Current Hermes profile ports are expected to be `default:8642`, `domi:8643`, `kun:8644`, `qianqian:8645`, and `serenity:8646`. `floria` can be healthy with `API_SERVER_ENABLED=false` and no local API port. When listing main and fallback models, inspect only safe fields from each profile: `model`, `providers`, `fallback_providers`, `API_SERVER_ENABLED`, and `API_SERVER_PORT`. The main model is `config.yaml` `model.provider` plus `model.default`; fallback models come from `fallback_providers`. Do not print full configs, headers, cookies, or tokens.

For Hermes TVRemix MCP availability, confirm `mcp_servers.tvremix` exists in relevant profile configs without displaying auth headers. Use `hermes -p <profile> mcp list` to confirm configuration and `hermes -p <profile> mcp test tvremix` to verify connection/tool discovery. If it fails, review only sanitized lines from `~/.hermes[/profiles/<profile>]/logs/mcp-stderr.log`.

For OpenClaw, check launchd, health, and listeners:

- `launchctl list | rg -i 'openclaw'`
- `curl -sS --max-time 2 http://127.0.0.1:18789/health`
- `lsof -nP -iTCP:18789 -iTCP:8789 -iTCP:3001 -iTCP:3002 -sTCP:LISTEN`

For Futu OpenD, confirm the app and local API port:

- `launchctl list | rg -i 'futu|opend'`
- `lsof -nP -iTCP:11111 -sTCP:LISTEN`
