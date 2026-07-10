from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
SCHEMA_VERSION = 2
WORKBUDDY_DUPLICATE_WINDOW_SECONDS = 300


def init_db(db_path: Path | str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _connect(path) as conn:
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
        conn.execute(
            "create table if not exists schema_meta (key text primary key, value text not null)"
        )
        _migrate_db(conn)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def upsert_records(db_path: Path | str, records: list[UsageRecord]) -> int:
    init_db(db_path)
    rows = [record.to_dict() for record in records]
    with _connect(db_path) as conn:
        before = conn.total_changes
        if rows:
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
                where usage_records.raw_json is not excluded.raw_json
                """,
                [{**row, "raw_json": json.dumps(row, ensure_ascii=False, sort_keys=True)} for row in rows],
            )
        changed = conn.total_changes - before
        _set_meta(conn, "last_ingest_at", str(time.time()))
        return changed


def delete_legacy_codex_records(db_path: Path | str) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
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
    with _connect(db_path) as conn:
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
    with _connect(db_path) as conn:
        before = conn.total_changes
        conn.execute(
            """
            delete from usage_records
            where input_tokens + output_tokens + cache_read_tokens
                + cache_write_tokens + reasoning_tokens <= 0
            """
        )
        return conn.total_changes - before


def delete_duplicate_workbuddy_records(db_path: Path | str) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        return _delete_duplicate_workbuddy_records(conn)


def load_records(db_path: Path | str, since: str | None = None) -> list[UsageRecord]:
    init_db(db_path)
    since_epoch = parse_since(since)
    where = "where timestamp >= ?" if since_epoch is not None else ""
    params = (since_epoch,) if since_epoch is not None else ()
    with _connect(db_path) as conn:
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
    timezone_name: str | None = None,
) -> list[dict[str, object]]:
    init_db(db_path)
    dims = _validate_group_by(group_by, ("agent",))
    timezone_value, _ = resolve_timezone(timezone_name)
    since_epoch = parse_since(since)
    where = "where timestamp >= ?" if since_epoch is not None else ""
    params = (since_epoch,) if since_epoch is not None else ()
    dim_select = "".join(
        ", normalize_model(model) as model" if name == "model" else f", {name}"
        for name in dims
    )
    dim_group = "".join(
        ", normalize_model(model)" if name == "model" else f", {name}"
        for name in dims
    )

    with _connect(db_path) as conn:
        conn.create_function(
            "usage_date",
            1,
            lambda value: datetime.fromtimestamp(float(value), timezone_value).date().isoformat(),
            deterministic=True,
        )
        conn.create_function("normalize_model", 1, normalize_model_name, deterministic=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                usage_date(timestamp) as usage_date
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


def hourly_summary_db(
    db_path: Path | str,
    since: str | None = None,
    group_by: tuple[str, ...] | None = None,
    timezone_name: str | None = None,
) -> list[dict[str, object]]:
    init_db(db_path)
    dims = _validate_group_by(group_by, ("agent",))
    timezone_value, _ = resolve_timezone(timezone_name)
    since_epoch = parse_since(since)
    where = "where timestamp >= ?" if since_epoch is not None else ""
    params = (since_epoch,) if since_epoch is not None else ()
    dim_select = "".join(
        ", normalize_model(model) as model" if name == "model" else f", {name}"
        for name in dims
    )
    dim_group = "".join(
        ", normalize_model(model)" if name == "model" else f", {name}"
        for name in dims
    )

    with _connect(db_path) as conn:
        conn.create_function(
            "usage_hour",
            1,
            lambda value: datetime.fromtimestamp(float(value), timezone_value).strftime(
                "%Y-%m-%dT%H:00"
            ),
            deterministic=True,
        )
        conn.create_function("normalize_model", 1, normalize_model_name, deterministic=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                usage_hour(timestamp) as usage_hour
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
            group by usage_hour{dim_group}
            order by usage_hour desc, total_tokens desc
            """,
            params,
        ).fetchall()

    return _merge_hourly_rows(rows, dims)


def database_metadata_db(db_path: Path | str, timezone_name: str | None = None) -> dict[str, object]:
    init_db(db_path)
    timezone_value, timezone_label = resolve_timezone(timezone_name)
    with _connect(db_path) as conn:
        latest_row = conn.execute("select max(timestamp) from usage_records").fetchone()
        ingest_row = conn.execute(
            "select value from schema_meta where key = 'last_ingest_at'"
        ).fetchone()
    return {
        "timezone": timezone_label,
        "currentDate": datetime.now(timezone_value).date().isoformat(),
        "latestTimestamp": float(latest_row[0]) if latest_row and latest_row[0] is not None else None,
        "lastIngestAt": float(ingest_row[0]) if ingest_row and ingest_row[0] else None,
    }


def resolve_timezone(name: str | None) -> tuple[tzinfo, str]:
    text = str(name or "").strip()
    if text:
        if len(text) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("invalid timezone")
        try:
            value = ZoneInfo(text)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {text}") from exc
        return value, text
    local = datetime.now().astimezone().tzinfo
    if local is None:
        value = ZoneInfo("UTC")
        return value, "UTC"
    return local, getattr(local, "key", None) or str(local)


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("pragma busy_timeout = 30000")
    conn.execute("pragma journal_mode = wal")
    return conn


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        insert into schema_meta (key, value) values (?, ?)
        on conflict(key) do update set value = excluded.value
        """,
        (key, value),
    )


def _migrate_db(conn: sqlite3.Connection) -> None:
    row = conn.execute("select value from schema_meta where key = 'schema_version'").fetchone()
    if row is None:
        existing_records = int(conn.execute("select count(*) from usage_records").fetchone()[0])
        version = 0 if existing_records else SCHEMA_VERSION
    else:
        try:
            version = int(row[0])
        except (TypeError, ValueError):
            version = 0

    if version < 1:
        _normalize_legacy_rows(conn, split_reasoning=False)
        version = 1
    if version < 2:
        _normalize_legacy_rows(conn, split_reasoning=True)
        _delete_duplicate_workbuddy_records(conn)
        version = 2
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )
    _set_meta(conn, "schema_version", str(version))


def _normalize_legacy_rows(conn: sqlite3.Connection, split_reasoning: bool) -> None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select record_id, host, agent, profile, source, session_id, provider, model,
               timestamp, started_at, ended_at, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens, reasoning_tokens,
               estimated_cost_usd, raw_json
        from usage_records
        """
    ).fetchall()
    updates: list[dict[str, object]] = []
    for row in rows:
        record = UsageRecord.from_dict(dict(row))
        if split_reasoning and record.agent != "codex" and record.reasoning_tokens:
            record = replace(
                record,
                output_tokens=max(record.output_tokens - record.reasoning_tokens, 0),
            )
        payload = record.to_dict()
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if (
            record.model != str(row["model"] or "")
            or record.output_tokens != int(row["output_tokens"] or 0)
            or raw_json != str(row["raw_json"] or "")
        ):
            updates.append(
                {
                    "record_id": record.record_id,
                    "model": record.model,
                    "output_tokens": record.output_tokens,
                    "raw_json": raw_json,
                }
            )
    if updates:
        conn.executemany(
            """
            update usage_records
            set model = :model,
                output_tokens = :output_tokens,
                raw_json = :raw_json
            where record_id = :record_id
            """,
            updates,
        )


def _delete_duplicate_workbuddy_records(conn: sqlite3.Connection) -> int:
    before = conn.total_changes
    conn.execute(
        """
        delete from usage_records
        where agent = 'workbuddy'
          and record_id like 'workbuddy:trace:%'
          and exists (
              select 1
              from usage_records as project
              where project.agent = 'workbuddy'
                and project.record_id not like 'workbuddy:trace:%'
                and project.host = usage_records.host
                and project.model = usage_records.model
                and project.input_tokens = usage_records.input_tokens
                and project.cache_read_tokens = usage_records.cache_read_tokens
                and project.cache_write_tokens = usage_records.cache_write_tokens
                and (
                    project.input_tokens + project.output_tokens + project.cache_read_tokens
                    + project.cache_write_tokens + project.reasoning_tokens
                ) = (
                    usage_records.input_tokens + usage_records.output_tokens
                    + usage_records.cache_read_tokens + usage_records.cache_write_tokens
                    + usage_records.reasoning_tokens
                )
                and abs(project.timestamp - usage_records.timestamp) <= ?
          )
        """,
        (WORKBUDDY_DUPLICATE_WINDOW_SECONDS,),
    )
    return conn.total_changes - before


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


def _merge_hourly_rows(rows: list[sqlite3.Row], dims: tuple[str, ...]) -> list[dict[str, object]]:
    buckets: dict[tuple[object, ...], dict[str, object]] = {}
    for sql_row in rows:
        hour = str(sql_row["usage_hour"] or "")
        values = tuple(_normalize_group_value(name, sql_row[name]) for name in dims)
        key = (hour, *values)
        if key not in buckets:
            row = {"hour": hour, "date": hour[:10]}
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
        bucket["estimated_cost_usd"] = float(bucket["estimated_cost_usd"]) + float(
            sql_row["estimated_cost_usd"] or 0
        )

    result = list(buckets.values())
    result.sort(key=lambda row: (str(row["hour"]), int(row["total_tokens"])), reverse=True)
    return result


def _normalize_group_value(name: str, value: object) -> str:
    if name == "model":
        return normalize_model_name(value)
    return str(value or "")
