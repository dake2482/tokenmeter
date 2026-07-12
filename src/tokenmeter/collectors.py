from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .records import UsageRecord, normalize_model_name

TOKEN_FIELDS = {
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
}
SUPPORTED_AGENTS = {"hermes", "openclaw", "codex", "zcode", "workbuddy", "claude", "claudecode"}
WORKBUDDY_DUPLICATE_WINDOW_SECONDS = 300


def collect_all(
    home: Path | str | None = None,
    host: str | None = None,
    since: float | None = None,
    agents: Iterable[str] | None = None,
    duplicate_record_ids: list[str] | None = None,
) -> list[UsageRecord]:
    home_path = Path(home or Path.home()).expanduser()
    host_name = host or socket.gethostname()
    wanted = {
        agent.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        for agent in (agents or ("hermes", "openclaw", "codex", "zcode", "workbuddy", "claude"))
    }
    unknown = sorted(wanted - SUPPORTED_AGENTS)
    if unknown:
        raise ValueError(f"unsupported agents: {', '.join(unknown)}")
    records: list[UsageRecord] = []
    if "hermes" in wanted:
        records.extend(collect_hermes(home_path, host_name, since))
    if "openclaw" in wanted:
        records.extend(collect_openclaw(home_path, host_name, since))
    if "codex" in wanted:
        records.extend(collect_codex(home_path, host_name, since, duplicate_record_ids))
    if "zcode" in wanted:
        records.extend(collect_zcode(home_path, host_name, since))
    if "workbuddy" in wanted:
        records.extend(collect_workbuddy(home_path, host_name, since))
    if "claude" in wanted or "claudecode" in wanted:
        records.extend(collect_claude_code(home_path, host_name, since))
    return records


def collect_hermes(home: Path, host: str, since: float | None = None) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    for profile, db_path in _discover_hermes_dbs(home):
        records.extend(_collect_hermes_db(profile, db_path, host, since))
    return records


def collect_openclaw(home: Path, host: str, since: float | None = None) -> list[UsageRecord]:
    root = home / ".openclaw" / "agents"
    if not root.exists():
        return []

    records: list[UsageRecord] = []
    for sessions_dir in sorted(root.glob("*/sessions")):
        profile = sessions_dir.parent.name
        for path in sorted(sessions_dir.glob("*.trajectory.jsonl")):
            if since is not None:
                try:
                    if path.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
            records.extend(_collect_openclaw_jsonl(path, profile, host, since))
    return records


def collect_codex(
    home: Path,
    host: str,
    since: float | None = None,
    duplicate_record_ids: list[str] | None = None,
) -> list[UsageRecord]:
    opentoken_bin = _opentoken_binary(home)
    collection_since = _local_day_start(since) if opentoken_bin is not None else since
    db_path = _newest_existing(
        home / ".codex" / "state_5.sqlite",
        home / ".codex" / "sqlite" / "state_5.sqlite",
    )
    if db_path is None:
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        cols = {row["name"] for row in conn.execute("pragma table_info(threads)")}
        required = {"id", "rollout_path", "updated_at"}
        if not required.issubset(cols):
            return []
        selected = sorted(cols & _CODEX_COLUMNS)
        where = "where updated_at >= ?" if collection_since is not None else ""
        params = (collection_since,) if collection_since is not None else ()
        rows = conn.execute(f"select {', '.join(selected)} from threads {where}", params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    records: list[UsageRecord] = []
    for row in rows:
        data = dict(row)
        rollout_path = Path(str(data.get("rollout_path") or ""))
        if not rollout_path.exists():
            continue
        if collection_since is not None and _mtime_before(rollout_path, collection_since):
            continue
        if _codex_forked_from(rollout_path):
            if duplicate_record_ids is not None:
                duplicate_record_ids.extend(
                    _codex_token_record_ids(rollout_path, str(data.get("id") or rollout_path.stem))
                )
            continue
        records.extend(
            _collect_codex_rollout_jsonl(
                rollout_path,
                data,
                host,
                collection_since,
                duplicate_record_ids,
            )
        )
    if opentoken_bin is not None:
        targets = _opentoken_codex_targets(opentoken_bin, home, collection_since)
        if targets is not None and (targets or not records):
            original_record_ids = {record.record_id for record in records}
            records = _reconcile_codex_records(records, targets, host)
            if duplicate_record_ids is not None:
                kept_record_ids = {record.record_id for record in records}
                duplicate_record_ids.extend(sorted(original_record_ids - kept_record_ids))
    return records


def collect_zcode(home: Path, host: str, since: float | None = None) -> list[UsageRecord]:
    db_path = home / ".zcode" / "cli" / "db" / "db.sqlite"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        cols = {row["name"] for row in conn.execute("pragma table_info(model_usage)")}
        required = {"id", "session_id", "completed_at"}
        if not required.issubset(cols):
            return []
        selected = sorted(cols & _ZCODE_COLUMNS)
        where = "where completed_at >= ?" if since is not None else ""
        params = (since * 1000,) if since is not None else ()
        rows = conn.execute(f"select {', '.join(selected)} from model_usage {where}", params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    records: list[UsageRecord] = []
    for row in rows:
        data = dict(row)
        timestamp = _parse_time(data.get("completed_at") or data.get("started_at"))
        if since is not None and timestamp < since:
            continue
        cache_read = _int(data.get("cache_read_input_tokens"))
        cache_write = _int(data.get("cache_creation_input_tokens"))
        raw_input = _int(data.get("input_tokens"))
        reasoning = _int(data.get("reasoning_tokens"))
        records.append(
            UsageRecord(
                record_id=f"zcode:{data.get('id')}",
                host=host,
                agent="zcode",
                profile=str(data.get("agent") or data.get("mode") or "default"),
                source=str(data.get("query_source") or ""),
                session_id=str(data.get("session_id") or ""),
                provider=str(data.get("provider_id") or ""),
                model=str(data.get("model_id") or ""),
                timestamp=timestamp,
                started_at=_parse_time(data.get("started_at")) if data.get("started_at") is not None else None,
                ended_at=timestamp,
                input_tokens=max(raw_input - cache_read - cache_write, 0),
                output_tokens=_exclusive_output_tokens(data.get("output_tokens"), reasoning),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                reasoning_tokens=reasoning,
            )
        )
    return records


def collect_workbuddy(home: Path, host: str, since: float | None = None) -> list[UsageRecord]:
    project_records: list[UsageRecord] = []
    root = home / ".workbuddy" / "projects"
    if root.exists():
        for path in sorted(root.glob("**/*.jsonl")):
            if since is not None and _mtime_before(path, since):
                continue
            project_records.extend(_collect_workbuddy_jsonl(path, host, since))

    trace_records: list[UsageRecord] = []
    trace_root = home / ".workbuddy" / "traces"
    if trace_root.exists():
        for path in sorted(trace_root.glob("**/*.json")):
            if since is not None and _mtime_before(path, since):
                continue
            trace_records.extend(_collect_workbuddy_trace_json(path, host, since))
    return [*project_records, *_deduplicate_workbuddy_traces(project_records, trace_records)]


def _deduplicate_workbuddy_traces(
    project_records: list[UsageRecord],
    trace_records: list[UsageRecord],
) -> list[UsageRecord]:
    candidates: dict[tuple[object, ...], list[tuple[int, UsageRecord]]] = {}
    for index, record in enumerate(project_records):
        candidates.setdefault(_workbuddy_usage_fingerprint(record), []).append((index, record))

    consumed: set[int] = set()
    unique_traces: list[UsageRecord] = []
    for trace_record in trace_records:
        matches = [
            (abs(project.timestamp - trace_record.timestamp), index)
            for index, project in candidates.get(_workbuddy_usage_fingerprint(trace_record), [])
            if index not in consumed
            and abs(project.timestamp - trace_record.timestamp) <= WORKBUDDY_DUPLICATE_WINDOW_SECONDS
        ]
        if matches:
            _, matched_index = min(matches)
            consumed.add(matched_index)
            continue
        unique_traces.append(trace_record)
    return unique_traces


def _workbuddy_usage_fingerprint(record: UsageRecord) -> tuple[object, ...]:
    return (
        record.model.lower(),
        record.input_tokens,
        record.cache_read_tokens,
        record.cache_write_tokens,
        record.total_tokens,
    )


def collect_claude_code(home: Path, host: str, since: float | None = None) -> list[UsageRecord]:
    root = home / ".claude" / "projects"
    if not root.exists():
        return []
    records: list[UsageRecord] = []
    for path in sorted(root.glob("**/*.jsonl")):
        if since is not None and _mtime_before(path, since):
            continue
        records.extend(_collect_claude_jsonl(path, host, since))
    return records


def parse_since(value: str | None, now: float | None = None) -> float | None:
    if value is None or value == "":
        return None
    text = value.strip().lower()
    if text in {"all", "0"}:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)

    match = re.fullmatch(r"(\d+(?:\.\d+)?)([mhdw])", text)
    if not match:
        raise ValueError(f"unsupported since value: {value}")
    amount = float(match.group(1))
    unit = match.group(2)
    seconds = {
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }[unit]
    return (now if now is not None else datetime.now(timezone.utc).timestamp()) - amount * seconds


def _discover_hermes_dbs(home: Path) -> list[tuple[str, Path]]:
    hermes = home / ".hermes"
    candidates: list[tuple[str, Path]] = []
    default_db = hermes / "state.db"
    if default_db.exists():
        candidates.append(("default", default_db))
    profiles_dir = hermes / "profiles"
    if profiles_dir.exists():
        for db_path in sorted(profiles_dir.glob("*/state.db")):
            candidates.append((db_path.parent.name, db_path))
    return candidates


def _collect_hermes_db(
    profile: str,
    db_path: Path,
    host: str,
    since: float | None,
) -> list[UsageRecord]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        cols = {row["name"] for row in conn.execute("pragma table_info(sessions)")}
        if not {"id", "started_at"}.issubset(cols):
            return []
        selected = sorted(cols & _HERMES_COLUMNS)
        timestamp_expression = "coalesce(ended_at, started_at)" if "ended_at" in cols else "started_at"
        where = f"where {timestamp_expression} >= ?" if since is not None else ""
        params = (since,) if since is not None else ()
        query = f"select {', '.join(selected)} from sessions {where}"
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    records: list[UsageRecord] = []
    for row in rows:
        data = dict(row)
        started_at = _float(data.get("started_at"))
        ended_at = _optional_float(data.get("ended_at"))
        reasoning = _int(data.get("reasoning_tokens"))
        total_tokens = sum(_int(data.get(field)) for field in TOKEN_FIELDS)
        if total_tokens <= 0:
            continue
        records.append(
            UsageRecord(
                record_id=f"hermes:{profile}:{data.get('id')}",
                host=host,
                agent="hermes",
                profile=profile,
                source=str(data.get("source") or ""),
                session_id=str(data.get("id") or ""),
                provider=str(data.get("billing_provider") or ""),
                model=str(data.get("model") or ""),
                timestamp=ended_at or started_at,
                started_at=started_at,
                ended_at=ended_at,
                input_tokens=_int(data.get("input_tokens")),
                output_tokens=_exclusive_output_tokens(data.get("output_tokens"), reasoning),
                cache_read_tokens=_int(data.get("cache_read_tokens")),
                cache_write_tokens=_int(data.get("cache_write_tokens")),
                reasoning_tokens=reasoning,
                estimated_cost_usd=_optional_float(data.get("estimated_cost_usd")),
            )
        )
    return records


_HERMES_COLUMNS = {
    "id",
    "source",
    "model",
    "started_at",
    "ended_at",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "billing_provider",
    "estimated_cost_usd",
}

_CODEX_COLUMNS = {
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "source",
    "thread_source",
    "model_provider",
    "cwd",
    "tokens_used",
    "agent_nickname",
    "model",
}

_ZCODE_COLUMNS = {
    "id",
    "session_id",
    "query_source",
    "provider_id",
    "model_id",
    "agent",
    "mode",
    "started_at",
    "completed_at",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
}


def _collect_codex_rollout_jsonl(
    path: Path,
    thread: dict,
    host: str,
    since: float | None,
    duplicate_record_ids: list[str] | None = None,
) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return []

    thread_id = str(thread.get("id") or path.stem)
    profile = _profile_from_cwd(thread.get("cwd")) or str(thread.get("agent_nickname") or "default")
    source = str(thread.get("thread_source") or thread.get("source") or "token_count")
    provider = str(thread.get("model_provider") or "openai")
    model = str(thread.get("model") or thread.get("model_provider") or "codex")

    previous_total_usage: int | None = None
    with handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = _as_dict(event.get("payload"))
            if event.get("type") == "turn_context":
                model = str(payload.get("model") or model)
                profile = _profile_from_cwd(payload.get("cwd")) or profile
                continue
            if payload.get("type") != "token_count":
                continue
            info = _as_dict(payload.get("info"))
            total_usage = _as_dict(info.get("total_token_usage"))
            cumulative_value = total_usage.get("total_tokens")
            cumulative_total = _int(cumulative_value) if cumulative_value is not None else None
            record_id = f"codex:{thread_id}:{line_no}"
            duplicate_snapshot = (
                cumulative_total is not None
                and previous_total_usage is not None
                and cumulative_total == previous_total_usage
            )
            if cumulative_total is not None:
                previous_total_usage = cumulative_total
            if duplicate_snapshot:
                if duplicate_record_ids is not None:
                    duplicate_record_ids.append(record_id)
                continue
            usage = _as_dict(info.get("last_token_usage"))
            if not usage:
                continue

            timestamp = _parse_time(event.get("timestamp"))
            if since is not None and timestamp < since:
                continue
            raw_input = _int(usage.get("input_tokens"))
            cache_read = _int(usage.get("cached_input_tokens"))
            raw_output = _int(usage.get("output_tokens"))
            reasoning = min(_int(usage.get("reasoning_output_tokens")), raw_output)
            if raw_input + raw_output <= 0:
                continue

            records.append(
                UsageRecord(
                    record_id=record_id,
                    host=host,
                    agent="codex",
                    profile=profile,
                    source=source,
                    session_id=thread_id,
                    provider=provider,
                    model=model,
                    timestamp=timestamp,
                    started_at=timestamp,
                    ended_at=timestamp,
                    input_tokens=max(raw_input - cache_read, 0),
                    output_tokens=max(raw_output - reasoning, 0),
                    cache_read_tokens=cache_read,
                    reasoning_tokens=reasoning,
                )
            )
    return records


def _codex_forked_from(path: Path) -> str | None:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "session_meta":
                continue
            payload = _as_dict(event.get("payload"))
            value = payload.get("forked_from_id")
            return str(value) if value else None
    return None


def _codex_token_record_ids(path: Path, thread_id: str) -> list[str]:
    record_ids: list[str] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return record_ids
    with handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = _as_dict(event.get("payload"))
            if payload.get("type") == "token_count":
                record_ids.append(f"codex:{thread_id}:{line_no}")
    return record_ids


def _opentoken_binary(home: Path) -> Path | None:
    path = home / ".local" / "bin" / "opentoken"
    return path if path.is_file() and os.access(path, os.X_OK) else None


def _local_day_start(timestamp: float | None) -> float | None:
    if timestamp is None:
        return None
    local = datetime.fromtimestamp(timestamp).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _opentoken_codex_targets(
    binary: Path,
    home: Path,
    since: float | None,
) -> dict[tuple[str, str], dict[str, int]] | None:
    command = [str(binary), "preview"]
    if since is not None:
        command.extend(["--since", datetime.fromtimestamp(since).astimezone().date().isoformat()])
    command.extend(["--tool", "codex", "--json"])
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home / ".codex")
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    targets: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("tool") != "codex":
            continue
        date = str(row.get("date") or "")
        model = normalize_model_name(row.get("model"))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not model:
            continue
        targets[(date, model)] = {
            "input": _int(row.get("input")),
            "cache_read": _int(row.get("cache_read")),
            "cache_write": _int(row.get("cache_write")),
            "output": _int(row.get("output")),
        }
    return targets


def _reconcile_codex_records(
    records: list[UsageRecord],
    targets: dict[tuple[str, str], dict[str, int]],
    host: str,
) -> list[UsageRecord]:
    grouped: dict[tuple[str, str], list[UsageRecord]] = {}
    for record in records:
        key = (datetime.fromtimestamp(record.timestamp).astimezone().date().isoformat(), record.model)
        grouped.setdefault(key, []).append(record)

    reconciled: list[UsageRecord] = []
    for key, target in sorted(targets.items()):
        bucket = grouped.get(key, [])
        if not bucket:
            reconciled.append(_synthetic_codex_record(key, target, host))
            continue
        input_values = _scale_integer_values([row.input_tokens for row in bucket], target["input"])
        cache_read_values = _scale_integer_values(
            [row.cache_read_tokens for row in bucket], target["cache_read"]
        )
        cache_write_values = _scale_integer_values(
            [row.cache_write_tokens for row in bucket], target["cache_write"]
        )
        combined_output = [row.output_tokens + row.reasoning_tokens for row in bucket]
        output_values = _scale_integer_values(combined_output, target["output"])
        for index, row in enumerate(bucket):
            base_output = combined_output[index]
            scaled_output = output_values[index]
            reasoning = (
                min(scaled_output, round(scaled_output * row.reasoning_tokens / base_output))
                if base_output
                else 0
            )
            reconciled.append(
                replace(
                    row,
                    input_tokens=input_values[index],
                    cache_read_tokens=cache_read_values[index],
                    cache_write_tokens=cache_write_values[index],
                    output_tokens=scaled_output - reasoning,
                    reasoning_tokens=reasoning,
                )
            )
    return [record for record in reconciled if record.total_tokens > 0]


def _scale_integer_values(values: list[int], target: int) -> list[int]:
    if not values:
        return []
    if target <= 0:
        return [0] * len(values)
    total = sum(values)
    if total <= 0:
        return [target, *([0] * (len(values) - 1))]
    scaled: list[int] = []
    remainders: list[tuple[int, int]] = []
    for index, value in enumerate(values):
        quotient, remainder = divmod(value * target, total)
        scaled.append(quotient)
        remainders.append((remainder, index))
    for _, index in sorted(remainders, reverse=True)[: target - sum(scaled)]:
        scaled[index] += 1
    return scaled


def _synthetic_codex_record(
    key: tuple[str, str],
    target: dict[str, int],
    host: str,
) -> UsageRecord:
    date, model = key
    digest = hashlib.sha256(f"{date}\0{model}".encode("utf-8")).hexdigest()[:20]
    timestamp = datetime.fromisoformat(f"{date}T12:00:00").astimezone().timestamp()
    return UsageRecord(
        record_id=f"codex:opentoken:{digest}",
        host=host,
        agent="codex",
        profile="default",
        source="opentoken-reconciled",
        session_id=f"opentoken:{date}:{model}",
        provider="openai",
        model=model,
        timestamp=timestamp,
        started_at=timestamp,
        ended_at=timestamp,
        input_tokens=target["input"],
        output_tokens=target["output"],
        cache_read_tokens=target["cache_read"],
        cache_write_tokens=target["cache_write"],
    )


def _collect_openclaw_jsonl(
    path: Path,
    profile: str,
    host: str,
    since: float | None,
) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return []

    with handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "model.completed":
                continue
            usage = event.get("data", {}).get("usage")
            if not isinstance(usage, dict):
                continue

            timestamp = _parse_ts(event.get("ts"))
            if since is not None and timestamp < since:
                continue
            session_id = str(event.get("sessionId") or path.stem.replace(".trajectory", ""))
            reasoning = _int(usage.get("reasoning"))
            output = _openclaw_output_tokens(usage, reasoning)
            records.append(
                UsageRecord(
                    record_id=f"openclaw:v2:{profile}:{path.name}:{line_no}",
                    host=host,
                    agent="openclaw",
                    profile=profile,
                    source=_openclaw_source(event),
                    session_id=session_id,
                    provider=str(event.get("provider") or ""),
                    model=str(event.get("modelId") or ""),
                    timestamp=timestamp,
                    started_at=timestamp,
                    ended_at=timestamp,
                    input_tokens=_int(usage.get("input")),
                    output_tokens=output,
                    cache_read_tokens=_int(usage.get("cacheRead")),
                    cache_write_tokens=_int(usage.get("cacheWrite")),
                    reasoning_tokens=reasoning,
                    estimated_cost_usd=_optional_float(
                        (usage.get("cost") or {}).get("total")
                        if isinstance(usage.get("cost"), dict)
                        else None
                    ),
                )
            )
    return records


def _collect_workbuddy_jsonl(path: Path, host: str, since: float | None) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return []

    with handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp = _parse_time(event.get("timestamp"))
            if since is not None and timestamp < since:
                continue
            usage = _workbuddy_usage(event)
            if not usage or sum(_int(usage.get(field)) for field in TOKEN_FIELDS) <= 0:
                continue

            provider_data = _as_dict(event.get("providerData"))
            record_id = event.get("id") or event.get("callId") or f"{path.stem}:{line_no}"
            records.append(
                UsageRecord(
                    record_id=f"workbuddy:{record_id}:{line_no}",
                    host=host,
                    agent="workbuddy",
                    profile=str(provider_data.get("agent") or _profile_from_cwd(event.get("cwd")) or "default"),
                    source=str(event.get("name") or event.get("type") or ""),
                    session_id=str(event.get("sessionId") or path.stem),
                    provider=str(provider_data.get("provider") or ""),
                    model=str(
                        provider_data.get("requestModelId")
                        or provider_data.get("model")
                        or provider_data.get("requestModelName")
                        or ""
                    ),
                    timestamp=timestamp,
                    started_at=timestamp,
                    ended_at=timestamp,
                    input_tokens=_int(usage.get("input_tokens")),
                    output_tokens=_int(usage.get("output_tokens")),
                    cache_read_tokens=_int(usage.get("cache_read_tokens")),
                    cache_write_tokens=_int(usage.get("cache_write_tokens")),
                    reasoning_tokens=_int(usage.get("reasoning_tokens")),
                )
            )
    return records


def _collect_workbuddy_trace_json(path: Path, host: str, since: float | None) -> list[UsageRecord]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    trace = _as_dict(data.get("trace"))
    spans = data.get("spans")
    if not isinstance(spans, list):
        return []

    records: list[UsageRecord] = []
    for index, item in enumerate(spans, start=1):
        span = _as_dict(item)
        if span.get("type") != "generation":
            continue

        timestamp = _parse_time(span.get("startedAt") or span.get("endedAt") or trace.get("startedAt"))
        if since is not None and timestamp < since:
            continue

        usage = _workbuddy_usage(span)
        model_from_output = ""
        if not usage:
            usage, model_from_output = _workbuddy_tool_output_usage(span.get("toolOutput"))
        if not usage or sum(_int(usage.get(field)) for field in TOKEN_FIELDS) <= 0:
            continue

        trace_id = str(trace.get("traceId") or span.get("traceId") or path.stem)
        span_id = str(span.get("spanId") or f"{path.stem}:{index}")
        records.append(
            UsageRecord(
                record_id=f"workbuddy:trace:{trace_id}:{span_id}",
                host=host,
                agent="workbuddy",
                profile=str(span.get("agent") or span.get("agentName") or "default"),
                source=str(span.get("name") or span.get("type") or "trace"),
                session_id=trace_id,
                provider=str(span.get("provider") or span.get("providerId") or ""),
                model=str(
                    span.get("requestModelId")
                    or span.get("model")
                    or span.get("requestModelName")
                    or model_from_output
                    or ""
                ),
                timestamp=timestamp,
                started_at=timestamp,
                ended_at=_parse_time(span.get("endedAt")) or timestamp,
                input_tokens=_int(usage.get("input_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                cache_read_tokens=_int(usage.get("cache_read_tokens")),
                cache_write_tokens=_int(usage.get("cache_write_tokens")),
                reasoning_tokens=_int(usage.get("reasoning_tokens")),
            )
        )
    return records


def _collect_claude_jsonl(path: Path, host: str, since: float | None) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return []

    with handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            message = _as_dict(event.get("message"))
            usage = _as_dict(message.get("usage"))
            if not usage:
                continue

            timestamp = _parse_time(event.get("timestamp"))
            if since is not None and timestamp < since:
                continue
            tokens = _usage_from_snake(usage, subtract_cache=False)
            if sum(_int(tokens.get(field)) for field in TOKEN_FIELDS) <= 0:
                continue

            record_id = message.get("id") or event.get("uuid") or f"{path.stem}:{line_no}"
            records.append(
                UsageRecord(
                    record_id=f"claude:{record_id}",
                    host=host,
                    agent="claude",
                    profile=_profile_from_cwd(event.get("cwd")) or path.parent.name or "default",
                    source=str(event.get("entrypoint") or event.get("userType") or ""),
                    session_id=str(event.get("sessionId") or path.stem),
                    provider="anthropic",
                    model=str(message.get("model") or ""),
                    timestamp=timestamp,
                    started_at=timestamp,
                    ended_at=timestamp,
                    input_tokens=_int(tokens.get("input_tokens")),
                    output_tokens=_int(tokens.get("output_tokens")),
                    cache_read_tokens=_int(tokens.get("cache_read_tokens")),
                    cache_write_tokens=_int(tokens.get("cache_write_tokens")),
                    reasoning_tokens=_int(tokens.get("reasoning_tokens")),
                )
            )
    return records


def _workbuddy_usage(event: dict) -> dict[str, int]:
    usage = _as_dict(event.get("usage"))
    if usage:
        return _usage_from_unknown(usage)

    message = _as_dict(event.get("message"))
    usage = _as_dict(message.get("usage"))
    if usage:
        return _usage_from_snake(usage, subtract_cache=True)

    provider_data = _as_dict(event.get("providerData"))
    usage = _as_dict(provider_data.get("usage"))
    if usage:
        return _usage_from_camel(usage)

    raw_usage = _as_dict(provider_data.get("rawUsage"))
    if raw_usage:
        return _usage_from_raw(raw_usage)
    return {}


def _workbuddy_tool_output_usage(value: object) -> tuple[dict[str, int], str]:
    if not isinstance(value, str) or not value.strip():
        return {}, ""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}, ""

    total = {field: 0 for field in TOKEN_FIELDS}
    model = ""
    for item, usage in _walk_usage_objects(payload):
        tokens = _usage_from_unknown(usage)
        if sum(_int(tokens.get(field)) for field in TOKEN_FIELDS) <= 0:
            continue
        for field in TOKEN_FIELDS:
            total[field] += _int(tokens.get(field))
        if not model:
            model = str(item.get("model") or item.get("modelId") or item.get("requestModelId") or "")
    if sum(total.values()) <= 0:
        return {}, ""
    return total, model


def _usage_from_unknown(usage: dict) -> dict[str, int]:
    if any(key in usage for key in ("prompt_tokens", "completion_tokens", "prompt_tokens_details")):
        return _usage_from_raw(usage)
    if any(key in usage for key in ("inputTokens", "outputTokens", "inputTokensDetails")):
        return _usage_from_camel(usage)
    return _usage_from_snake(usage, subtract_cache=True)


def _usage_from_snake(usage: dict, subtract_cache: bool) -> dict[str, int]:
    cache_read = _int(usage.get("cache_read_tokens") or usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_write_tokens") or usage.get("cache_creation_input_tokens"))
    raw_input = _int(usage.get("input_tokens"))
    reasoning = _int(usage.get("reasoning_tokens") or usage.get("reasoning_output_tokens"))
    input_tokens = max(raw_input - cache_read - cache_write, 0) if subtract_cache else raw_input
    return {
        "input_tokens": input_tokens,
        "output_tokens": _exclusive_output_tokens(usage.get("output_tokens"), reasoning),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
    }


def _usage_from_camel(usage: dict) -> dict[str, int]:
    input_details = usage.get("inputTokensDetails") or usage.get("input_tokens_details")
    output_details = usage.get("outputTokensDetails") or usage.get("output_tokens_details")
    cache_read = _sum_details(input_details, "cached_tokens", "cache_read_tokens", "cacheReadTokens")
    cache_write = _sum_details(
        input_details,
        "cache_creation_input_tokens",
        "cache_write_tokens",
        "cacheWriteTokens",
    )
    raw_input = _int(usage.get("inputTokens") or usage.get("input_tokens") or usage.get("promptTokens"))
    reasoning = _sum_details(output_details, "reasoning_tokens", "reasoningTokens")
    return {
        "input_tokens": max(raw_input - cache_read - cache_write, 0),
        "output_tokens": _exclusive_output_tokens(
            usage.get("outputTokens") or usage.get("output_tokens") or usage.get("completionTokens"),
            reasoning,
        ),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
    }


def _usage_from_raw(usage: dict) -> dict[str, int]:
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cache_read = _int(usage.get("cache_read_input_tokens") or usage.get("prompt_cache_hit_tokens"))
    cache_read = cache_read or _sum_details(prompt_details, "cached_tokens", "cache_read_tokens")
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    cache_write = cache_write or _sum_details(prompt_details, "cache_creation_input_tokens", "cache_write_tokens")
    raw_input = _int(usage.get("input_tokens") or usage.get("prompt_tokens"))
    reasoning = _sum_details(completion_details, "reasoning_tokens")
    return {
        "input_tokens": max(raw_input - cache_read - cache_write, 0),
        "output_tokens": _exclusive_output_tokens(
            usage.get("output_tokens") or usage.get("completion_tokens"),
            reasoning,
        ),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
    }


def _exclusive_output_tokens(output_value: object, reasoning_tokens: int) -> int:
    return max(_int(output_value) - reasoning_tokens, 0)


def _openclaw_output_tokens(usage: dict, reasoning_tokens: int) -> int:
    output = _int(usage.get("output"))
    if reasoning_tokens <= 0:
        return output
    declared_total = _int(usage.get("total") or usage.get("totalTokens"))
    total_without_reasoning = (
        _int(usage.get("input"))
        + output
        + _int(usage.get("cacheRead"))
        + _int(usage.get("cacheWrite"))
    )
    if declared_total and declared_total == total_without_reasoning + reasoning_tokens:
        return output
    return max(output - reasoning_tokens, 0)


def _sum_details(details: object, *keys: str) -> int:
    if isinstance(details, list):
        return sum(_sum_details(item, *keys) for item in details)
    if not isinstance(details, dict):
        return 0
    for key in keys:
        value = _int(details.get(key))
        if value:
            return value
    return 0


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _walk_usage_objects(value: object):
    if isinstance(value, dict):
        usage = _as_dict(value.get("usage"))
        if usage:
            yield value, usage
            return
        for child in value.values():
            yield from _walk_usage_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_usage_objects(child)


def _openclaw_source(event: dict) -> str:
    session_key = str(event.get("sessionKey") or "")
    parts = session_key.split(":")
    if len(parts) >= 4 and parts[0] == "agent":
        return parts[2]
    source = event.get("source")
    if source:
        return str(source)
    return ""


def _parse_time(value: object) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return _normalize_timestamp(number)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            number = float(text)
            return _normalize_timestamp(number)
        except ValueError:
            return _parse_ts(text)
    return 0


def _profile_from_cwd(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    name = Path(value).expanduser().name
    return name if name and name != "." else value


def _mtime_before(path: Path, since: float) -> bool:
    try:
        return path.stat().st_mtime < since
    except OSError:
        return True


def _newest_existing(*paths: Path) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    try:
        return max(existing, key=lambda path: path.stat().st_mtime)
    except OSError:
        return None


def _parse_ts(value: object) -> float:
    if isinstance(value, (int, float)):
        return _normalize_timestamp(float(value))
    if not isinstance(value, str) or not value:
        return 0
    try:
        return _normalize_timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        number = float(value or 0)
        return number if math.isfinite(number) and number >= 0 else 0
    except (TypeError, ValueError):
        return 0


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _normalize_timestamp(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return 0
    return value / 1000 if value > 10_000_000_000 else value
