from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo
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
FIVE_HOUR_WINDOW_SECONDS = 5 * 3600
CAPACITY_LOOKBACK_DAYS = 60


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


def dashboard_daily_summary_db(
    db_path: Path | str,
    since: str | None = None,
    group_by: tuple[str, ...] | None = None,
    timezone_name: str | None = None,
) -> list[dict[str, object]]:
    """Return the lean daily rows used by the dashboard's initial payload."""
    init_db(db_path)
    dims = _validate_group_by(group_by, ("agent",))
    timezone_value, _ = resolve_timezone(timezone_name)
    since_epoch = parse_since(since)
    where = "where timestamp >= ?" if since_epoch is not None else ""
    where_params: tuple[float, ...] = (since_epoch,) if since_epoch is not None else ()
    dim_select = "".join(f", {name}" for name in dims)
    dim_group = "".join(f", {name}" for name in dims)

    with _connect(db_path) as conn:
        bounds = conn.execute(
            f"select min(timestamp), max(timestamp) from usage_records {where}",
            where_params,
        ).fetchone()
        start_epoch = float(bounds[0]) if bounds and bounds[0] is not None else time.time()
        end_epoch = float(bounds[1]) if bounds and bounds[1] is not None else start_epoch
        fixed_offset = _fixed_utc_offset_seconds(timezone_value, start_epoch, end_epoch)
        conn.row_factory = sqlite3.Row
        if fixed_offset is not None:
            date_select = "cast((timestamp + ?) / 86400 as integer) as usage_day"
            date_group = "usage_day"
            params: tuple[object, ...] = (fixed_offset, *where_params)
        else:
            conn.create_function(
                "usage_date",
                1,
                lambda value: datetime.fromtimestamp(float(value), timezone_value).date().isoformat(),
                deterministic=True,
            )
            date_select = "usage_date(timestamp) as usage_date"
            date_group = "usage_date"
            params = where_params
        rows = conn.execute(
            f"""
            select
                {date_select}
                {dim_select},
                sum(input_tokens + output_tokens + cache_read_tokens
                    + cache_write_tokens + reasoning_tokens) as total_tokens,
                coalesce(sum(estimated_cost_usd), 0.0) as estimated_cost_usd
            from usage_records
            {where}
            group by {date_group}{dim_group}
            order by {date_group} desc, total_tokens desc
            """,
            params,
        ).fetchall()

    return _merge_dashboard_daily_rows(rows, dims, fixed_offset is not None)


def hourly_summary_db(
    db_path: Path | str,
    since: str | None = None,
    group_by: tuple[str, ...] | None = None,
    timezone_name: str | None = None,
) -> list[dict[str, object]]:
    rows = interval_summary_db(
        db_path,
        since=since,
        group_by=group_by,
        timezone_name=timezone_name,
        interval_minutes=60,
    )
    for row in rows:
        row["hour"] = row.pop("interval")
    return rows


def interval_summary_db(
    db_path: Path | str,
    since: str | None = None,
    group_by: tuple[str, ...] | None = None,
    timezone_name: str | None = None,
    interval_minutes: int = 15,
) -> list[dict[str, object]]:
    init_db(db_path)
    dims = _validate_group_by(group_by, ("agent",))
    timezone_value, _ = resolve_timezone(timezone_name)
    if interval_minutes <= 0 or interval_minutes > 60 or 60 % interval_minutes:
        raise ValueError("interval_minutes must be a positive divisor of 60")
    since_epoch = parse_since(since)
    where = "where timestamp >= ?" if since_epoch is not None else ""
    bucket_seconds = interval_minutes * 60
    offset = datetime.now(timezone_value).utcoffset()
    bucket_phase = int(offset.total_seconds() if offset else 0) % bucket_seconds
    where_params = (since_epoch,) if since_epoch is not None else ()
    params = (bucket_phase, bucket_seconds, *where_params)
    dim_select = "".join(
        ", normalize_model(model) as model" if name == "model" else f", {name}"
        for name in dims
    )
    dim_group = "".join(
        ", normalize_model(model)" if name == "model" else f", {name}"
        for name in dims
    )

    with _connect(db_path) as conn:
        conn.create_function("normalize_model", 1, normalize_model_name, deterministic=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                cast((timestamp + ?) / ? as integer) as usage_bucket
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
            group by usage_bucket{dim_group}
            order by usage_bucket desc, total_tokens desc
            """,
            params,
        ).fetchall()

    prepared_rows: list[dict[str, object]] = []
    for row in rows:
        prepared = dict(row)
        bucket_epoch = int(prepared["usage_bucket"]) * bucket_seconds - bucket_phase
        prepared["usage_interval"] = _usage_interval(
            bucket_epoch,
            timezone_value,
            interval_minutes,
        )
        prepared_rows.append(prepared)
    return _merge_interval_rows(prepared_rows, dims)


def five_hour_capacity_db(
    db_path: Path | str,
    now: float | None = None,
    lookback_days: int = CAPACITY_LOOKBACK_DAYS,
) -> list[dict[str, object]]:
    init_db(db_path)
    current_time = time.time() if now is None else float(now)
    if not math.isfinite(current_time) or current_time <= 0:
        raise ValueError("now must be a positive finite timestamp")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")

    since_epoch = current_time - lookback_days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            select
                agent,
                model,
                timestamp,
                input_tokens + output_tokens + cache_read_tokens
                    + cache_write_tokens + reasoning_tokens as total_tokens
            from usage_records
            where (agent = 'codex' or lower(model) like '%glm%')
                and timestamp >= ? and timestamp <= ?
            order by timestamp
            """,
            (since_epoch, current_time),
        ).fetchall()

    by_scope: dict[str, list[tuple[float, int]]] = {"codex": [], "glm": []}
    for agent, model, timestamp, total_tokens in rows:
        event = (float(timestamp), int(total_tokens or 0))
        if str(agent) == "codex":
            by_scope["codex"].append(event)
        if normalize_model_name(model).upper().startswith("GLM-"):
            by_scope["glm"].append(event)

    return [
        _five_hour_scope_capacity(scope, by_scope[scope], current_time, lookback_days)
        for scope in ("codex", "glm")
    ]


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


def _merge_dashboard_daily_rows(
    rows: list[sqlite3.Row],
    dims: tuple[str, ...],
    numeric_date: bool,
) -> list[dict[str, object]]:
    epoch = datetime(1970, 1, 1)
    buckets: dict[tuple[object, ...], dict[str, object]] = {}
    for sql_row in rows:
        usage_date = (
            (epoch + timedelta(days=int(sql_row["usage_day"]))).date().isoformat()
            if numeric_date
            else str(sql_row["usage_date"] or "")
        )
        values = tuple(_normalize_group_value(name, sql_row[name]) for name in dims)
        key = (usage_date, *values)
        if key not in buckets:
            row = {"date": usage_date}
            row.update({name: value for name, value in zip(dims, values)})
            row.update({"total_tokens": 0, "estimated_cost_usd": 0.0})
            buckets[key] = row
        bucket = buckets[key]
        bucket["total_tokens"] = int(bucket["total_tokens"]) + int(sql_row["total_tokens"] or 0)
        bucket["estimated_cost_usd"] = float(bucket["estimated_cost_usd"]) + float(
            sql_row["estimated_cost_usd"] or 0
        )
    result = list(buckets.values())
    result.sort(key=lambda row: (str(row["date"]), int(row["total_tokens"])), reverse=True)
    return result


def _merge_interval_rows(
    rows: list[sqlite3.Row | dict[str, object]],
    dims: tuple[str, ...],
) -> list[dict[str, object]]:
    buckets: dict[tuple[object, ...], dict[str, object]] = {}
    for sql_row in rows:
        interval = str(sql_row["usage_interval"] or "")
        values = tuple(_normalize_group_value(name, sql_row[name]) for name in dims)
        key = (interval, *values)
        if key not in buckets:
            row = {"interval": interval, "date": interval[:10]}
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
    result.sort(key=lambda row: (str(row["interval"]), int(row["total_tokens"])), reverse=True)
    return result


def _usage_interval(value: object, timezone_value: tzinfo, interval_minutes: int) -> str:
    moment = datetime.fromtimestamp(float(value), timezone_value)
    minute = moment.minute // interval_minutes * interval_minutes
    return moment.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _fixed_utc_offset_seconds(
    timezone_value: tzinfo,
    start_epoch: float,
    end_epoch: float,
) -> int | None:
    start = min(start_epoch, end_epoch)
    end = max(start_epoch, end_epoch)
    expected: int | None = None
    cursor = start
    while True:
        offset = datetime.fromtimestamp(cursor, timezone_value).utcoffset()
        seconds = int(offset.total_seconds() if offset else 0)
        if expected is None:
            expected = seconds
        elif seconds != expected:
            return None
        if cursor >= end:
            return expected
        cursor = min(cursor + 6 * 3600, end)


def _five_hour_scope_capacity(
    scope: str,
    events: list[tuple[float, int]],
    now: float,
    lookback_days: int,
) -> dict[str, object]:
    cutoff = now - FIVE_HOUR_WINDOW_SECONDS
    current_events = [(timestamp, tokens) for timestamp, tokens in events if timestamp > cutoff]
    current_tokens = sum(tokens for _timestamp, tokens in current_events)
    next_release_at = (
        min(timestamp for timestamp, _tokens in current_events) + FIVE_HOUR_WINDOW_SECONDS
        if current_events
        else None
    )

    left = 0
    rolling_tokens = 0
    observed_peak_tokens = 0
    observed_peak_start_at: float | None = None
    observed_peak_end_at: float | None = None
    for right, (timestamp, tokens) in enumerate(events):
        rolling_tokens += tokens
        while left <= right and events[left][0] <= timestamp - FIVE_HOUR_WINDOW_SECONDS:
            rolling_tokens -= events[left][1]
            left += 1
        if rolling_tokens > observed_peak_tokens:
            observed_peak_tokens = rolling_tokens
            observed_peak_start_at = events[left][0] if left <= right else timestamp
            observed_peak_end_at = timestamp

    return {
        "scope": scope,
        "windowMinutes": FIVE_HOUR_WINDOW_SECONDS // 60,
        "currentTokens": current_tokens,
        "observedPeakTokens": observed_peak_tokens,
        "remainingToPeakTokens": max(observed_peak_tokens - current_tokens, 0),
        "windowStartedAt": cutoff,
        "nextReleaseAt": next_release_at,
        "observedPeakStartAt": observed_peak_start_at,
        "observedPeakEndAt": observed_peak_end_at,
        "lookbackDays": lookback_days,
        "method": "observed_rolling_peak",
    }


def _normalize_group_value(name: str, value: object) -> str:
    if name == "model":
        return normalize_model_name(value)
    return str(value or "")
