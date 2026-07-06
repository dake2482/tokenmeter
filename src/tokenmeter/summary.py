from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .records import UsageRecord

DEFAULT_GROUP_BY = ("host", "agent", "profile", "source", "provider", "model")
TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def summarize_records(
    records: Iterable[UsageRecord],
    group_by: tuple[str, ...] = DEFAULT_GROUP_BY,
) -> list[dict[str, object]]:
    buckets: dict[tuple[object, ...], dict[str, object]] = {}
    for record in records:
        key = tuple(getattr(record, name) for name in group_by)
        if key not in buckets:
            buckets[key] = {name: value for name, value in zip(group_by, key)}
            buckets[key].update(
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
        bucket = buckets[key]
        bucket["records"] = int(bucket["records"]) + 1
        bucket["sessions"].add(record.session_id)
        for column in TOKEN_COLUMNS:
            bucket[column] = int(bucket[column]) + int(getattr(record, column))
        if record.estimated_cost_usd:
            bucket["estimated_cost_usd"] = float(bucket["estimated_cost_usd"]) + record.estimated_cost_usd

    rows: list[dict[str, object]] = []
    for bucket in buckets.values():
        bucket["sessions"] = len(bucket["sessions"])
        rows.append(bucket)
    rows.sort(key=lambda row: int(row["total_tokens"]), reverse=True)
    return rows


def format_table(rows: list[dict[str, object]], columns: tuple[str, ...] | None = None) -> str:
    if not rows:
        return "No usage records found."
    columns = columns or (
        "host",
        "agent",
        "profile",
        "source",
        "provider",
        "model",
        "sessions",
        "records",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    widths = {
        col: max(len(col), *(len(_cell(row.get(col))) for row in rows))
        for col in columns
    }
    header = "  ".join(col.ljust(widths[col]) for col in columns)
    rule = "  ".join("-" * widths[col] for col in columns)
    body = [
        "  ".join(_cell(row.get(col)).rjust(widths[col]) if _is_number(row.get(col)) else _cell(row.get(col)).ljust(widths[col]) for col in columns)
        for row in rows
    ]
    return "\n".join([header, rule, *body])


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float)

