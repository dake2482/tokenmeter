from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .collectors import parse_since
from .records import UsageRecord
from .summary import summarize_records


def init_db(db_path: Path | str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table if not exists usage_records (
                record_id text primary key,
                host text not null,
                agent text not null,
                profile text not null,
                source text not null,
                session_id text not null,
                provider text not null,
                model text not null,
                timestamp real not null,
                started_at real,
                ended_at real,
                input_tokens integer not null,
                output_tokens integer not null,
                cache_read_tokens integer not null,
                cache_write_tokens integer not null,
                reasoning_tokens integer not null,
                estimated_cost_usd real,
                raw_json text not null
            )
            """
        )
        conn.execute("create index if not exists idx_usage_time on usage_records(timestamp)")
        conn.execute("create index if not exists idx_usage_dims on usage_records(host, agent, profile, model)")


def upsert_records(db_path: Path | str, records: list[UsageRecord]) -> int:
    init_db(db_path)
    rows = [record.to_dict() for record in records]
    with sqlite3.connect(db_path) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            insert into usage_records (
                record_id, host, agent, profile, source, session_id, provider, model,
                timestamp, started_at, ended_at,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                reasoning_tokens, estimated_cost_usd, raw_json
            ) values (
                :record_id, :host, :agent, :profile, :source, :session_id, :provider, :model,
                :timestamp, :started_at, :ended_at,
                :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens,
                :reasoning_tokens, :estimated_cost_usd, :raw_json
            )
            on conflict(record_id) do update set
                host=excluded.host,
                agent=excluded.agent,
                profile=excluded.profile,
                source=excluded.source,
                session_id=excluded.session_id,
                provider=excluded.provider,
                model=excluded.model,
                timestamp=excluded.timestamp,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                cache_read_tokens=excluded.cache_read_tokens,
                cache_write_tokens=excluded.cache_write_tokens,
                reasoning_tokens=excluded.reasoning_tokens,
                estimated_cost_usd=excluded.estimated_cost_usd,
                raw_json=excluded.raw_json
            """,
            [{**row, "raw_json": json.dumps(row, ensure_ascii=False, sort_keys=True)} for row in rows],
        )
        return conn.total_changes - before


def load_records(db_path: Path | str, since: str | None = None) -> list[UsageRecord]:
    init_db(db_path)
    since_epoch = parse_since(since)
    where = "where timestamp >= ?" if since_epoch is not None else ""
    params = (since_epoch,) if since_epoch is not None else ()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select record_id, host, agent, profile, source, session_id, provider, model,
                   timestamp, started_at, ended_at, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   estimated_cost_usd
            from usage_records
            {where}
            """,
            params,
        ).fetchall()
    return [UsageRecord.from_dict(dict(row)) for row in rows]


def summarize_db(
    db_path: Path | str,
    since: str | None = None,
    group_by: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    records = load_records(db_path, since=since)
    return summarize_records(records, group_by or ("host", "agent", "profile", "source", "provider", "model"))


def daily_summary_db(
    db_path: Path | str,
    since: str | None = None,
    group_by: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    records = load_records(db_path, since=since)
    dims = group_by or ("agent",)
    buckets: dict[tuple[object, ...], dict[str, object]] = {}
    for record in records:
        date = datetime.fromtimestamp(record.timestamp).date().isoformat()
        key = (date, *(getattr(record, name) for name in dims))
        if key not in buckets:
            row = {"date": date}
            row.update({name: value for name, value in zip(dims, key[1:])})
            row.update(
                {
                    "records": 0,
                    "sessions": set(),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                }
            )
            buckets[key] = row
        bucket = buckets[key]
        bucket["records"] = int(bucket["records"]) + 1
        bucket["sessions"].add(record.session_id)
        bucket["input_tokens"] = int(bucket["input_tokens"]) + record.input_tokens
        bucket["output_tokens"] = int(bucket["output_tokens"]) + record.output_tokens
        bucket["cache_read_tokens"] = int(bucket["cache_read_tokens"]) + record.cache_read_tokens
        bucket["cache_write_tokens"] = int(bucket["cache_write_tokens"]) + record.cache_write_tokens
        bucket["reasoning_tokens"] = int(bucket["reasoning_tokens"]) + record.reasoning_tokens
        bucket["total_tokens"] = int(bucket["total_tokens"]) + record.total_tokens
        if record.estimated_cost_usd:
            bucket["estimated_cost_usd"] = float(bucket["estimated_cost_usd"]) + record.estimated_cost_usd

    rows = []
    for row in buckets.values():
        row["sessions"] = len(row["sessions"])
        rows.append(row)
    rows.sort(key=lambda row: (str(row["date"]), int(row["total_tokens"])), reverse=True)
    return rows
