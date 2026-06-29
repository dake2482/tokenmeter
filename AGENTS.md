# Repository Guidelines

## Shared Policy

Also follow `/Users/dake/Documents/AGENT_POLICY.md`. This file adds Server-specific repository guidance and local service health-check rules.

## Project Structure & Module Organization

This repository is currently a fresh Git project with no application files committed yet. Keep future code organized by purpose:

- `src/` for production source code.
- `tests/` for automated tests that mirror `src/` paths where practical.
- `assets/` for static files such as images, fixtures, or sample data.
- `docs/` for design notes, runbooks, and user-facing documentation.
- `scripts/` for repeatable local or CI helper commands.

Avoid placing generated build output, dependency folders, credentials, or local editor state in the repository.

## Build, Test, and Development Commands

No build or test toolchain is configured yet. When one is added, document the canonical commands here and keep them runnable from the repository root. Examples:

- `npm install` or equivalent: install project dependencies.
- `npm run dev`: start the local development server.
- `npm test`: run the full automated test suite.
- `npm run build`: create a production build.

Prefer adding these commands to a standard project manifest, such as `package.json`, `Makefile`, or language-specific build config.

## Coding Style & Naming Conventions

Use consistent formatting within each language ecosystem. Prefer automated formatters and linters over manual style rules, and commit their configuration with the code. Use descriptive names: `kebab-case` for scripts and static assets, `camelCase` for JavaScript/TypeScript variables, and `PascalCase` for component or class names where applicable.

## Testing Guidelines

Add tests with each behavioral change. Name tests after the unit or workflow they verify, such as `tests/server_config.test.ts` or `tests/test_server_config.py`. Keep fixtures small and deterministic. The default test command should run without external services unless documented otherwise.

## Commit & Pull Request Guidelines

There is no existing commit history, so use clear imperative commit messages such as `Add server config loader`. Pull requests should include a short summary, validation steps, linked issues when relevant, and screenshots or logs for user-visible behavior.

## Local Service Health Checks

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

## Security & Configuration Tips

Never commit secrets, API keys, certificates, or machine-specific `.env` files. Commit safe examples instead, such as `.env.example`, and document required variables in `docs/` or this file.
