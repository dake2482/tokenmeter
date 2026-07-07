from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .collectors import parse_since
from .records import UsageRecord, normalize_model_name
from .summary import summarize_records

GROUP_BY_COLUMNS = {"host", "agent", "profile", "source", "provider", "model"}
TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


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


def delete_legacy_codex_records(db_path: Path | str) -> int:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        before = conn.total_changes
        conn.execute(
            """
            delete from usage_records
            where agent = 'codex'
              and record_id not like 'codex:%:%'
            """
        )
        return conn.total_changes - before


def delete_legacy_openclaw_records(db_path: Path | str) -> int:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        before = conn.total_changes
        conn.execute(
            """
            delete from usage_records
            where agent = 'openclaw'
              and record_id not like 'openclaw:v2:%'
            """
        )
        return conn.total_changes - before


def delete_zero_token_records(db_path: Path | str) -> int:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        before = conn.total_changes
        conn.execute(
            """
            delete from usage_records
            where input_tokens + output_tokens + cache_read_tokens
                + cache_write_tokens + reasoning_tokens <= 0
            """
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
    dims = _validate_group_by(group_by, ("host", "agent", "profile", "source", "provider", "model"))
    return summarize_records(records, dims)


def daily_summary_db(
    db_path: Path | str,
    since: str | None = None,
    group_by: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    init_db(db_path)
    dims = _validate_group_by(group_by, ("agent",))
    since_epoch = parse_since(since)
    where = "where timestamp >= ?" if since_epoch is not None else ""
    params = (since_epoch,) if since_epoch is not None else ()
    dim_select = "".join(f", {name}" for name in dims)
    dim_group = "".join(f", {name}" for name in dims)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                date(timestamp, 'unixepoch', 'localtime') as usage_date
                {dim_select},
                count(*) as records,
                count(distinct session_id) as sessions,
                sum(input_tokens) as input_tokens,
                sum(output_tokens) as output_tokens,
                sum(cache_read_tokens) as cache_read_tokens,
                sum(cache_write_tokens) as cache_write_tokens,
                sum(reasoning_tokens) as reasoning_tokens,
                sum(input_tokens + output_tokens + cache_read_tokens
                    + cache_write_tokens + reasoning_tokens) as total_tokens,
                coalesce(sum(estimated_cost_usd), 0.0) as estimated_cost_usd
            from usage_records
            {where}
            group by usage_date{dim_group}
            order by usage_date desc, total_tokens desc
            """,
            params,
        ).fetchall()

    return _merge_daily_rows(rows, dims)


def _validate_group_by(group_by: tuple[str, ...] | None, default: tuple[str, ...]) -> tuple[str, ...]:
    dims = group_by or default
    invalid = [name for name in dims if name not in GROUP_BY_COLUMNS]
    if invalid:
        raise ValueError(f"unsupported group_by column: {', '.join(invalid)}")
    return dims


def _merge_daily_rows(rows: list[sqlite3.Row], dims: tuple[str, ...]) -> list[dict[str, object]]:
    buckets: dict[tuple[object, ...], dict[str, object]] = {}
    for sql_row in rows:
        date = str(sql_row["usage_date"] or "")
        values = tuple(_normalize_group_value(name, sql_row[name]) for name in dims)
        key = (date, *values)
        if key not in buckets:
            row = {"date": date}
            row.update({name: value for name, value in zip(dims, key[1:])})
            row.update(
                {
                    "records": 0,
                    "sessions": 0,
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
        bucket["records"] = int(bucket["records"]) + int(sql_row["records"] or 0)
        bucket["sessions"] = int(bucket["sessions"]) + int(sql_row["sessions"] or 0)
        for column in TOKEN_COLUMNS:
            bucket[column] = int(bucket[column]) + int(sql_row[column] or 0)
        bucket["total_tokens"] = int(bucket["total_tokens"]) + int(sql_row["total_tokens"] or 0)
        bucket["estimated_cost_usd"] = float(bucket["estimated_cost_usd"]) + float(sql_row["estimated_cost_usd"] or 0)

    rows = list(buckets.values())
    rows.sort(key=lambda row: (str(row["date"]), int(row["total_tokens"])), reverse=True)
    return rows


def _normalize_group_value(name: str, value: object) -> str:
    if name == "model":
        return normalize_model_name(value)
    return str(value or "")
