from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UsageRecord:
    record_id: str
    host: str
    agent: str
    profile: str
    source: str
    session_id: str
    provider: str
    model: str
    timestamp: float
    started_at: float | None = None
    ended_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", normalize_model_name(self.model))

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.reasoning_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "host": self.host,
            "agent": self.agent,
            "profile": self.profile,
            "source": self.source,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "timestamp": self.timestamp,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsageRecord":
        return cls(
            record_id=str(data["record_id"]),
            host=str(data.get("host") or ""),
            agent=str(data.get("agent") or ""),
            profile=str(data.get("profile") or ""),
            source=str(data.get("source") or ""),
            session_id=str(data.get("session_id") or ""),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            timestamp=float(data.get("timestamp") or 0),
            started_at=_optional_float(data.get("started_at")),
            ended_at=_optional_float(data.get("ended_at")),
            input_tokens=_int(data.get("input_tokens")),
            output_tokens=_int(data.get("output_tokens")),
            cache_read_tokens=_int(data.get("cache_read_tokens")),
            cache_write_tokens=_int(data.get("cache_write_tokens")),
            reasoning_tokens=_int(data.get("reasoning_tokens")),
            estimated_cost_usd=_optional_float(data.get("estimated_cost_usd")),
        )


def normalize_model_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lower = text.lower()
    separated_glm = re.fullmatch(r"glm[-_ ]?(\d+)[._-](\d+)", lower)
    if separated_glm:
        return f"GLM-{separated_glm.group(1)}.{separated_glm.group(2)}"
    separated_gpt = re.fullmatch(r"gpt[-_ ]?(\d+)[._-](\d+)(.*)", lower)
    if separated_gpt:
        return f"GPT-{separated_gpt.group(1)}.{separated_gpt.group(2)}{separated_gpt.group(3)}"

    compact = re.sub(r"[^a-z0-9]", "", lower)
    compact_glm = re.fullmatch(r"glm(\d)(\d+)", compact)
    if compact_glm:
        return f"GLM-{compact_glm.group(1)}.{compact_glm.group(2)}"
    compact_gpt = re.fullmatch(r"gpt(\d)(\d+)", compact)
    if compact_gpt:
        return f"GPT-{compact_gpt.group(1)}.{compact_gpt.group(2)}"
    return text


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
