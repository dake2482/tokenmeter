from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

from .collectors import collect_all, parse_since
from .server import run_server
from .storage import (
    delete_legacy_codex_records,
    delete_legacy_openclaw_records,
    delete_zero_token_records,
    summarize_db,
    upsert_records,
)
from .summary import DEFAULT_GROUP_BY, format_table, summarize_records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokenmeter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="collect local token usage")
    _add_collect_args(collect_parser)
    collect_parser.add_argument("--format", choices=("table", "json"), default="table")

    upload_parser = subparsers.add_parser("upload", help="collect and upload to a tokenmeter server")
    _add_collect_args(upload_parser)
    upload_parser.add_argument("--server", required=True, help="base URL, e.g. http://127.0.0.1:18888")
    upload_parser.add_argument("--token", default=os.environ.get("TOKENMETER_TOKEN"))

    serve_parser = subparsers.add_parser("serve", help="run the aggregation HTTP server")
    serve_parser.add_argument("--bind", default="127.0.0.1:18888")
    serve_parser.add_argument("--db", default="data/tokenmeter.sqlite")
    serve_parser.add_argument("--token", default=os.environ.get("TOKENMETER_TOKEN"))
    serve_parser.add_argument(
        "--auto-import-interval",
        default="15m",
        help="run local import in the background every interval, e.g. 15m; use 0 to disable",
    )
    serve_parser.add_argument(
        "--auto-import-since",
        default="1d",
        help="collection window for each background import, e.g. 1d",
    )
    serve_parser.add_argument("--auto-import-home", default=str(Path.home()))
    serve_parser.add_argument("--auto-import-host", default=socket.gethostname())
    serve_parser.add_argument(
        "--auto-import-agents",
        default="hermes,openclaw,codex,zcode,workbuddy,claude",
        help="comma-separated agents for background import",
    )

    import_parser = subparsers.add_parser("import", help="store local collection in a central SQLite DB")
    _add_collect_args(import_parser)
    import_parser.add_argument("--db", default="data/tokenmeter.sqlite")

    summary_parser = subparsers.add_parser("summary", help="summarize records from a central SQLite DB")
    summary_parser.add_argument("--db", default="data/tokenmeter.sqlite")
    summary_parser.add_argument("--since", default="7d")
    summary_parser.add_argument("--group-by", default=",".join(DEFAULT_GROUP_BY))
    summary_parser.add_argument("--format", choices=("table", "json"), default="table")

    args = parser.parse_args(argv)
    if args.command == "collect":
        return _cmd_collect(args)
    if args.command == "upload":
        return _cmd_upload(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "summary":
        return _cmd_summary(args)
    return 2


def _add_collect_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", default=str(Path.home()), help="home directory to scan")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--since", default="7d", help="relative window such as 24h, 7d, 4w, all")
    parser.add_argument(
        "--agents",
        default="hermes,openclaw,codex,zcode,workbuddy,claude",
        help="comma-separated: hermes,openclaw,codex,zcode,workbuddy,claude",
    )


def _cmd_collect(args: argparse.Namespace) -> int:
    records = _collect_from_args(args)
    rows = summarize_records(records)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "host": args.host,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "records": [record.to_dict() for record in records],
                    "summary": rows,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(format_table(rows))
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    records = _collect_from_args(args)
    payload = json.dumps({"records": [record.to_dict() for record in records]}, ensure_ascii=False).encode("utf-8")
    url = args.server.rstrip("/") + "/api/v1/usage"
    req = request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    if args.token:
        req.add_header("Authorization", f"Bearer {args.token}")
    with request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    print(body)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    host, port = _parse_bind(args.bind)
    auto_import_agents = tuple(
        agent.strip() for agent in args.auto_import_agents.split(",") if agent.strip()
    )
    run_server(
        host,
        port,
        Path(args.db),
        args.token,
        auto_import_interval_seconds=_parse_duration_seconds(args.auto_import_interval),
        auto_import_since=args.auto_import_since,
        auto_import_home=Path(args.auto_import_home),
        auto_import_host=args.auto_import_host,
        auto_import_agents=auto_import_agents,
    )
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    records = _collect_from_args(args)
    agents = {record.agent for record in records}
    cleaned = delete_zero_token_records(args.db)
    if "codex" in agents:
        cleaned += delete_legacy_codex_records(args.db)
    if "openclaw" in agents:
        cleaned += delete_legacy_openclaw_records(args.db)
    changed = upsert_records(args.db, records)
    print(f"stored {len(records)} records ({changed + cleaned} changed, {cleaned} legacy cleaned) in {args.db}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    group_by = tuple(part.strip() for part in args.group_by.split(",") if part.strip())
    rows = summarize_db(args.db, since=args.since, group_by=group_by)
    if args.format == "json":
        print(json.dumps({"rows": rows}, ensure_ascii=False, sort_keys=True))
    else:
        print(format_table(rows))
    return 0


def _collect_from_args(args: argparse.Namespace):
    since = parse_since(args.since)
    agents = [agent.strip() for agent in args.agents.split(",") if agent.strip()]
    return collect_all(home=Path(args.home), host=args.host, since=since, agents=agents)


def _parse_bind(value: str) -> tuple[str, int]:
    if ":" not in value:
        return value, 18888
    host, port = value.rsplit(":", 1)
    return host, int(port)


def _parse_duration_seconds(value: str | int | float | None) -> float:
    if value is None:
        return 0
    if isinstance(value, int | float):
        return float(value)
    text = value.strip().lower()
    if not text:
        return 0
    if text in {"0", "off", "false", "none", "disabled"}:
        return 0
    if text[-1:] in {"s", "m", "h", "d"}:
        amount = float(text[:-1])
        return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]]
    return float(text)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
