from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tokenmeter.__main__ import _parse_duration_seconds
from tokenmeter.collectors import collect_all, parse_since
from tokenmeter.records import UsageRecord
from tokenmeter.server import _asset_name_for_path, _dashboard_payload, _manifest_payload, _strip_app_prefix
from tokenmeter.storage import daily_summary_db, interval_summary_db, upsert_records
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

    def test_parse_duration_seconds(self) -> None:
        self.assertEqual(_parse_duration_seconds("15m"), 900)
        self.assertEqual(_parse_duration_seconds("1h"), 3600)
        self.assertEqual(_parse_duration_seconds("0"), 0)

    def test_glm_model_name_variants_are_normalized_for_summary(self) -> None:
        records = [
            _usage_record("a", "glm5.2", 10),
            _usage_record("b", "GLM-5.2", 20),
            _usage_record("c", "glm-5.2", 30),
        ]

        rows = summarize_records(records, group_by=("model",))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "GLM-5.2")
        self.assertEqual(rows[0]["total_tokens"], 60)

    def test_gpt_model_name_variants_are_normalized_for_summary(self) -> None:
        records = [
            _usage_record("a", "gpt5.5", 10),
            _usage_record("b", "GPT-5.5", 20),
            _usage_record("c", "gpt-5.5", 30),
        ]

        rows = summarize_records(records, group_by=("model",))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "GPT-5.5")
        self.assertEqual(rows[0]["total_tokens"], 60)

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

    def test_daily_summary_normalizes_legacy_model_names_after_sql_grouping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tokenmeter.sqlite"
            upsert_records(
                db_path,
                [
                    _usage_record("a", "glm5.2", 10),
                    _usage_record("b", "GLM-5.2", 20),
                ],
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute("update usage_records set model = 'glm5.2' where record_id = 'a'")

            rows = daily_summary_db(db_path, since="all", group_by=("model",))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "GLM-5.2")
        self.assertEqual(rows[0]["total_tokens"], 30)

    def test_interval_summary_groups_real_records_by_quarter_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tokenmeter.sqlite"
            upsert_records(
                db_path,
                [
                    _usage_record("a", "GPT-5.5", 10, timestamp=1_000, agent="codex"),
                    _usage_record("b", "GPT-5.5", 20, timestamp=1_800, agent="codex"),
                    _usage_record("c", "GPT-5.5", 30, timestamp=1_850, agent="codex"),
                    _usage_record("d", "GLM-5.2", 40, timestamp=3_700, agent="hermes"),
                ],
            )

            rows = interval_summary_db(
                db_path,
                since="all",
                group_by=("agent",),
                timezone_name="UTC",
                interval_minutes=15,
            )

        by_interval_agent = {(row["interval"], row["agent"]): row for row in rows}
        self.assertEqual(by_interval_agent[("1970-01-01T00:15", "codex")]["total_tokens"], 10)
        self.assertEqual(by_interval_agent[("1970-01-01T00:30", "codex")]["total_tokens"], 50)
        self.assertEqual(by_interval_agent[("1970-01-01T01:00", "hermes")]["total_tokens"], 40)
        self.assertEqual(by_interval_agent[("1970-01-01T01:00", "hermes")]["date"], "1970-01-01")

    def test_dashboard_payload_combines_home_page_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _make_hermes_db(home / ".hermes" / "state.db", "s-default", 1000, 20)
            _make_openclaw_jsonl(home / ".openclaw" / "agents" / "main" / "sessions" / "run.trajectory.jsonl")
            db_path = home / "tokenmeter.sqlite"
            upsert_records(db_path, collect_all(home=home, host="test-host", since=0))

            payload = _dashboard_payload(db_path, since="all")

        self.assertEqual(
            set(payload),
            {"dailyByTool", "dailyByModel", "dailyByAgentModel", "dailyByHost", "intervalByTool", "meta"},
        )
        self.assertTrue(payload["dailyByTool"])
        self.assertTrue(payload["dailyByModel"])
        self.assertTrue(payload["dailyByHost"])
        self.assertIsInstance(payload["intervalByTool"], list)

    def test_openclaw_duplicate_seq_events_have_distinct_record_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _make_openclaw_duplicate_seq_jsonl(
                home / ".openclaw" / "agents" / "main" / "sessions" / "dup.trajectory.jsonl"
            )

            records = collect_all(home=home, host="test-host", since=0, agents=["openclaw"])

        self.assertEqual(len(records), 2)
        self.assertEqual(len({record.record_id for record in records}), 2)
        self.assertEqual(sum(record.total_tokens for record in records), 30)

    def test_collects_codex_zcode_workbuddy_and_claude_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _make_codex_db(home / ".codex" / "sqlite" / "state_5.sqlite")
            _make_zcode_db(home / ".zcode" / "cli" / "db" / "db.sqlite")
            _make_workbuddy_jsonl(home / ".workbuddy" / "projects" / "Users-alice-Project" / "run.jsonl")
            _make_claude_jsonl(home / ".claude" / "projects" / "-Users-alice-Project" / "run.jsonl")

            records = collect_all(home=home, host="test-host", since=0)

        by_agent = {record.agent: record for record in records}
        self.assertEqual(set(by_agent), {"codex", "zcode", "workbuddy", "claude"})
        self.assertEqual(by_agent["codex"].total_tokens, 1000)
        self.assertEqual(by_agent["codex"].profile, "Project")
        self.assertEqual(by_agent["zcode"].total_tokens, 120)
        self.assertEqual(by_agent["zcode"].input_tokens, 60)
        self.assertEqual(by_agent["workbuddy"].total_tokens, 340)
        self.assertEqual(by_agent["workbuddy"].cache_read_tokens, 100)
        self.assertEqual(by_agent["claude"].total_tokens, 67)

    def test_collects_workbuddy_trace_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _make_workbuddy_trace_json(home / ".workbuddy" / "traces" / "123" / "trace_wb.json")

            records = collect_all(home=home, host="test-host", since=0, agents=["workbuddy"])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].record_id, "workbuddy:trace:trace-wb:span-generation-1")
        self.assertEqual(records[0].profile, "default")
        self.assertEqual(records[0].model, "GPT-5.5")
        self.assertEqual(records[0].input_tokens, 70)
        self.assertEqual(records[0].cache_read_tokens, 20)
        self.assertEqual(records[0].output_tokens, 20)
        self.assertEqual(records[0].reasoning_tokens, 10)

    def test_codex_uses_newest_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old_db = home / ".codex" / "sqlite" / "state_5.sqlite"
            new_db = home / ".codex" / "state_5.sqlite"
            _make_codex_db(old_db, token_count=1000, updated_at=1000)
            _make_codex_db(new_db, token_count=3000, updated_at=2000)
            os.utime(old_db, (1000, 1000))
            os.utime(new_db, (2000, 2000))

            records = collect_all(home=home, host="test-host", since=0, agents=["codex"])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].total_tokens, 3000)
        self.assertEqual(records[0].timestamp, 2000)

    def test_web_app_icon_paths_support_tokenmeter_prefix(self) -> None:
        self.assertEqual(_strip_app_prefix("/tokenmeter"), "/")
        self.assertEqual(_strip_app_prefix("/tokenmeter/assets/tokenmeter-plain-t-icon.svg"), "/assets/tokenmeter-plain-t-icon.svg")
        self.assertEqual(_asset_name_for_path("/favicon.svg"), "tokenmeter-plain-t-icon.svg")
        self.assertEqual(_asset_name_for_path("/assets/apple-touch-icon-plain-t.png"), "apple-touch-icon-plain-t.png")

        manifest = _manifest_payload()

        self.assertEqual(manifest["start_url"], "/tokenmeter")
        self.assertEqual(manifest["scope"], "/tokenmeter/")
        self.assertIn("/tokenmeter/assets/tokenmeter-plain-t-icon-512.png", {icon["src"] for icon in manifest["icons"]})

    def test_install_script_has_valid_shell_syntax(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        subprocess.run(["sh", "-n", "scripts/install.sh"], cwd=repo_root, check=True)

    def test_readme_does_not_include_private_deployment_addresses(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        private_addresses = (
            ".".join(("43", "159", "50", "227")),
            ".".join(("192", "168", "3", "227")),
        )

        for address in private_addresses:
            self.assertNotIn(address, readme)


def _usage_record(
    record_id: str,
    model: str,
    tokens: int,
    timestamp: float = 0,
    agent: str = "test",
) -> UsageRecord:
    return UsageRecord(
        record_id=record_id,
        host="test-host",
        agent=agent,
        profile="default",
        source="test",
        session_id=record_id,
        provider="test",
        model=model,
        timestamp=timestamp,
        input_tokens=tokens,
    )


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


def _make_openclaw_duplicate_seq_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "model.completed",
            "seq": 5,
            "ts": "1970-01-01T00:00:02Z",
            "provider": "zai",
            "modelId": "glm-test",
            "sessionId": "oc-dup",
            "sessionKey": "agent:main:cron:x:run:oc-dup",
            "data": {"usage": {"input": 10, "output": 5}},
        },
        {
            "type": "model.completed",
            "seq": 5,
            "ts": "1970-01-01T00:00:03Z",
            "provider": "zai",
            "modelId": "glm-test",
            "sessionId": "oc-dup",
            "sessionKey": "agent:main:cron:x:run:oc-dup",
            "data": {"usage": {"input": 10, "output": 5}},
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")


def _make_codex_db(path: Path, token_count: int = 1000, updated_at: int = 1010) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    codex_root = path.parent.parent if path.parent.name == "sqlite" else path.parent
    rollout_path = codex_root / "sessions" / f"rollout-{updated_at}.jsonl"
    _make_codex_rollout_jsonl(rollout_path, token_count, updated_at)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text,
                created_at integer,
                updated_at integer,
                source text,
                thread_source text,
                model_provider text,
                cwd text,
                tokens_used integer,
                agent_nickname text,
                model text
            )
            """
        )
        conn.execute(
            """
            insert into threads values (
                'codex-thread-1', ?, 1000, ?, 'vscode', 'codex_desktop', 'openai',
                '/Users/alice/Project', ?, null, 'gpt-test'
            )
            """,
            (str(rollout_path), updated_at, token_count),
        )


def _make_codex_rollout_jsonl(path: Path, token_count: int, timestamp: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output_tokens = min(20, token_count)
    reasoning_tokens = min(5, output_tokens)
    input_tokens = max(token_count - output_tokens, 0)
    event = {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": min(40, input_tokens),
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_tokens,
                    "total_tokens": token_count,
                }
            },
        },
    }
    path.write_text(json.dumps(event), encoding="utf-8")


def _make_zcode_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table model_usage (
                id text primary key,
                session_id text,
                query_source text,
                provider_id text,
                model_id text,
                agent text,
                mode text,
                started_at integer,
                completed_at integer,
                input_tokens integer,
                output_tokens integer,
                reasoning_tokens integer,
                cache_creation_input_tokens integer,
                cache_read_input_tokens integer
            )
            """
        )
        conn.execute(
            """
            insert into model_usage values (
                'zcode-usage-1', 'z-session-1', 'main_turn', 'builtin:test',
                'glm-test', 'zcode-agent', 'yolo', 1000000, 1001000,
                100, 20, 0, 0, 40
            )
            """
        )


def _make_workbuddy_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "id": "wb-1",
        "timestamp": 1_002_000,
        "type": "assistant",
        "sessionId": "wb-session-1",
        "cwd": "/Users/alice/Project",
        "providerData": {
            "agent": "default",
            "requestModelId": "work-model",
            "usage": {
                "inputTokens": 300,
                "outputTokens": 40,
                "inputTokensDetails": [{"cached_tokens": 100}],
                "outputTokensDetails": [{"reasoning_tokens": 5}],
            },
        },
    }
    path.write_text(json.dumps(event), encoding="utf-8")


def _make_workbuddy_trace_json(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "model": "gpt-5.5",
            "usage": {
                "prompt_tokens": 90,
                "completion_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        }
    ]
    event = {
        "trace": {
            "traceId": "trace-wb",
            "startedAt": "1970-01-01T00:16:40Z",
        },
        "spans": [
            {
                "spanId": "span-ignored",
                "type": "function",
                "startedAt": "1970-01-01T00:16:40Z",
                "toolOutput": json.dumps(payload),
            },
            {
                "spanId": "span-generation-1",
                "type": "generation",
                "name": "generation",
                "startedAt": "1970-01-01T00:16:41Z",
                "endedAt": "1970-01-01T00:16:42Z",
                "toolOutput": json.dumps(payload),
            },
        ],
    }
    path.write_text(json.dumps(event), encoding="utf-8")


def _make_claude_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "uuid": "claude-event-1",
        "timestamp": "1970-01-01T00:16:43Z",
        "entrypoint": "cli",
        "cwd": "/Users/alice/Project",
        "sessionId": "claude-session-1",
        "message": {
            "id": "msg-1",
            "model": "claude-test",
            "usage": {
                "input_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "output_tokens": 7,
            },
        },
    }
    path.write_text(json.dumps(event), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
