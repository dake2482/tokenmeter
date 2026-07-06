from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tokenmeter.collectors import collect_all, parse_since
from tokenmeter.storage import daily_summary_db, upsert_records
from tokenmeter.summary import summarize_records


class TokenMeterTests(unittest.TestCase):
    def test_collects_hermes_profiles_and_openclaw_model_completed_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _make_hermes_db(home / ".hermes" / "state.db", "s-default", 1000, 20)
            _make_hermes_db(home / ".hermes" / "profiles" / "kun" / "state.db", "s-kun", 2000, 40)
            _make_openclaw_jsonl(home / ".openclaw" / "agents" / "main" / "sessions" / "run.trajectory.jsonl")

            records = collect_all(home=home, host="test-host", since=0)

        self.assertEqual(len(records), 3)
        self.assertEqual({record.agent for record in records}, {"hermes", "openclaw"})
        self.assertIn(("hermes", "default"), {(record.agent, record.profile) for record in records})
        self.assertIn(("hermes", "kun"), {(record.agent, record.profile) for record in records})
        self.assertIn(("openclaw", "main"), {(record.agent, record.profile) for record in records})
        openclaw = next(record for record in records if record.agent == "openclaw")
        self.assertEqual(openclaw.source, "cron")
        self.assertEqual(openclaw.total_tokens, 17)

    def test_summary_groups_by_host_agent_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _make_hermes_db(home / ".hermes" / "state.db", "s-default", 1000, 20)
            records = collect_all(home=home, host="h1", since=0)

        rows = summarize_records(records, group_by=("host", "agent", "profile"))

        self.assertEqual(rows[0]["host"], "h1")
        self.assertEqual(rows[0]["agent"], "hermes")
        self.assertEqual(rows[0]["profile"], "default")
        self.assertEqual(rows[0]["total_tokens"], 23)

    def test_parse_since_relative_window(self) -> None:
        self.assertEqual(parse_since("2h", now=10_000), 2_800)
        self.assertIsNone(parse_since("all", now=10_000))

    def test_daily_summary_groups_records_by_date_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _make_hermes_db(home / ".hermes" / "state.db", "s-default", 1000, 20)
            _make_openclaw_jsonl(home / ".openclaw" / "agents" / "main" / "sessions" / "run.trajectory.jsonl")
            records = collect_all(home=home, host="test-host", since=0)
            db_path = home / "tokenmeter.sqlite"
            upsert_records(db_path, records)

            rows = daily_summary_db(db_path, since="all", group_by=("agent",))

        by_agent = {row["agent"]: row for row in rows}
        self.assertEqual(by_agent["hermes"]["date"], "1970-01-01")
        self.assertEqual(by_agent["hermes"]["total_tokens"], 23)
        self.assertEqual(by_agent["openclaw"]["total_tokens"], 17)


def _make_hermes_db(path: Path, session_id: str, started_at: float, input_tokens: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table sessions (
                id text primary key,
                source text,
                model text,
                started_at real,
                ended_at real,
                input_tokens integer,
                output_tokens integer,
                cache_read_tokens integer,
                cache_write_tokens integer,
                reasoning_tokens integer,
                billing_provider text,
                estimated_cost_usd real
            )
            """
        )
        conn.execute(
            """
            insert into sessions values (
                ?, 'cli', 'glm-test', ?, ?, ?, 2, 1, 0, 0, 'zai', 0.001
            )
            """,
            (session_id, started_at, started_at + 5, input_tokens),
        )


def _make_openclaw_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "session.started",
            "ts": "1970-01-01T00:00:01Z",
            "sessionId": "oc-1",
            "sessionKey": "agent:main:cron:x:run:oc-1",
        },
        {
            "type": "model.completed",
            "seq": 2,
            "ts": "1970-01-01T00:00:02Z",
            "provider": "zai",
            "modelId": "glm-test",
            "sessionId": "oc-1",
            "sessionKey": "agent:main:cron:x:run:oc-1",
            "data": {"usage": {"input": 10, "output": 5, "cacheRead": 2}},
        },
        {
            "type": "trace.artifacts",
            "ts": "1970-01-01T00:00:03Z",
            "sessionId": "oc-1",
            "data": {"usage": {"input": 10_000, "output": 10_000}},
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
