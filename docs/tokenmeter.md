# TokenMeter

TokenMeter is a small self-hosted token usage collector for the machines that run local agents. It borrows the useful part of TokenRank's install shape: one command installs a local collector, binds it to an upload endpoint, performs a first upload, then schedules future uploads.

This repository implements the collector and the central HTTP receiver first. The installer can be layered on top once the target server URL is fixed.

## What The TokenRank Pattern Does

The reviewed installer script does four things:

1. Accepts an upload or subscription URL as its only argument.
2. Detects OS and CPU architecture, then downloads the matching binary.
3. Runs `connect`, `upload`, and `service install` through that binary.
4. Leaves the binary under `$HOME/.local/bin` and tells the user where to view results.

For our own servers, the equivalent shape should be:

```sh
curl -fsSL https://your-domain.example/tokenmeter/install.sh | sh -s -- "https://your-domain.example/api/v1/usage?token=..."
```

The local collector should store the upload URL/token in a private ignored config file, then run `tokenmeter upload` on a timer.

## Data Model

Every usage row has these dimensions:

- `host`: physical or cloud machine name.
- `agent`: runtime family, such as `hermes`, `openclaw`, `codex`, `zcode`, `workbuddy`, or `claude`.
- `profile`: Hermes profile name, OpenClaw agent directory, or a best-effort profile derived from cwd / agent metadata.
- `source`: channel or trigger, such as `cli`, `cron`, `discord`, or `weixin` when available.
- `provider` and `model`: provider/model observed at runtime.
- token columns: input, output, cache read, cache write, reasoning, and total.

Hermes is read from each `state.db` `sessions` table. TokenMeter only reads aggregate columns and does not read `messages.content`.

OpenClaw is read from `~/.openclaw/agents/*/sessions/*.trajectory.jsonl`. TokenMeter counts only `model.completed` events and ignores duplicate `trace.artifacts` usage snapshots.

Codex uses `~/.codex/state_5.sqlite` as the thread index, then reads each rollout JSONL `token_count.info.last_token_usage` entry. It does not use `threads.tokens_used` for daily usage because that field is cumulative per thread.

ZCode is read from `~/.zcode/cli/db/db.sqlite` `model_usage` rows. Cached tokens are split out from input tokens so totals do not double count cache.

WorkBuddy is read from `~/.workbuddy/projects/**/*.jsonl` usage metadata.

Claude Code is read from `~/.claude/projects/**/*.jsonl` message usage metadata.

The JSONL collectors read token usage and runtime metadata only. They do not read or persist message content or prompts.

## Local Commands

Run a local summary:

```sh
PYTHONPATH=src python3 -m tokenmeter collect --since 7d
```

Store the current machine's collection into a local central DB:

```sh
PYTHONPATH=src python3 -m tokenmeter import --db data/tokenmeter.sqlite --since 7d
PYTHONPATH=src python3 -m tokenmeter summary --db data/tokenmeter.sqlite --since 7d
```

Run the central receiver:

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter serve --bind 127.0.0.1:18888 --db data/tokenmeter.sqlite
```

Open the web dashboard:

```text
http://127.0.0.1:18888/
```

Upload from another machine:

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter upload --server http://127.0.0.1:18888 --host "$(hostname)" --since 7d
```

Fetch server-side summary:

```sh
curl -H "Authorization: Bearer $TOKENMETER_TOKEN" \
  "http://127.0.0.1:18888/api/v1/summary?since=7d&group_by=host,agent,profile,model"
```

## Deployment Notes

- Do not commit upload tokens, local SQLite DBs, logs, or generated reports.
- Use a long random `TOKENMETER_TOKEN` for the receiver.
- Keep the receiver private behind SSH, Tailscale, or a reverse proxy with HTTPS.
- To expose the web page on another server, bind the receiver to a non-loopback address:

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter serve \
  --bind 0.0.0.0:18888 \
  --db /var/lib/tokenmeter/tokenmeter.sqlite
```

- Then open `http://<server-ip>:18888/` and enter the same token in the page.
- Start with hourly uploads. The collector is idempotent because each row has a stable `record_id`.
- For Linux hosts, run `tokenmeter upload` from a user-level systemd timer. For macOS hosts, run it from a LaunchAgent.
