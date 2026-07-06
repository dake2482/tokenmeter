from __future__ import annotations

import json
import re
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .records import UsageRecord

TOKEN_FIELDS = {
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
}


def collect_all(
    home: Path | str | None = None,
    host: str | None = None,
    since: float | None = None,
    agents: Iterable[str] | None = None,
) -> list[UsageRecord]:
    home_path = Path(home or Path.home()).expanduser()
    host_name = host or socket.gethostname()
    wanted = {agent.strip().lower() for agent in (agents or ("hermes", "openclaw"))}
    records: list[UsageRecord] = []
    if "hermes" in wanted:
        records.extend(collect_hermes(home_path, host_name, since))
    if "openclaw" in wanted:
        records.extend(collect_openclaw(home_path, host_name, since))
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
        where = "where started_at >= ?" if since is not None else ""
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
                output_tokens=_int(data.get("output_tokens")),
                cache_read_tokens=_int(data.get("cache_read_tokens")),
                cache_write_tokens=_int(data.get("cache_write_tokens")),
                reasoning_tokens=_int(data.get("reasoning_tokens")),
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
            seq = event.get("seq") if event.get("seq") is not None else line_no
            records.append(
                UsageRecord(
                    record_id=f"openclaw:{profile}:{session_id}:{seq}",
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
                    output_tokens=_int(usage.get("output")),
                    cache_read_tokens=_int(usage.get("cacheRead")),
                    cache_write_tokens=_int(usage.get("cacheWrite")),
                    reasoning_tokens=_int(usage.get("reasoning")),
                    estimated_cost_usd=_optional_float(
                        (usage.get("cost") or {}).get("total")
                        if isinstance(usage.get("cost"), dict)
                        else None
                    ),
                )
            )
    return records


def _openclaw_source(event: dict) -> str:
    session_key = str(event.get("sessionKey") or "")
    parts = session_key.split(":")
    if len(parts) >= 4 and parts[0] == "agent":
        return parts[2]
    source = event.get("source")
    if source:
        return str(source)
    return ""


def _parse_ts(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
