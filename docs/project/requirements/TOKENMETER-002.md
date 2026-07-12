---
schema_version: 1
id: TOKENMETER-002
project_id: TOKENMETER
title: Verify Codex usage accounting
status: REVIEW
priority: P1
executor: codex
task_id: local-audit-20260712
branch: main
worktree: local
dependencies: []
updated_at: '2026-07-12T09:18:00+08:00'
next_action: Deploy the committed fix, run an all-history upload, and verify the corrected central database totals.
evidence:
- Independent 24-hour audit matched collector token fields for every retained event and found repeated cumulative snapshots.
- At the 2026-07-12 audit snapshot, production reported 2,716,949,772 Codex tokens for the day while independent deduplication removed 128,521,248 duplicated tokens, a 4.97 percent overstatement.
- Full local replay identified 6,223 duplicate record IDs.
- Temporary-database reconciliation produced exactly 42,288 Codex records and 6,357,548,806 tokens in both collector output and SQLite.
- All 21 unit and integration tests plus Python and installer syntax checks passed.
---

# Intent

Audit the Codex statistics end to end without reading or retaining prompt or response content. Verify that TokenMeter records incremental `token_count.last_token_usage` values once, assigns them to the event date, and preserves the same totals in SQLite.

## Acceptance criteria

- [x] Raw Codex usage events and collector output match by event identity and token fields.
- [x] Collector output and TokenMeter SQLite records match without missing or duplicate records.
- [x] Daily Codex totals match an independent raw-event aggregation.
- [x] Any scope limitation, uncertainty, or accounting defect is documented with reproducible evidence.

## Verification evidence

- The collector now skips `token_count` snapshots whose `total_token_usage.total_tokens` value is unchanged from the previous snapshot in the same rollout.
- Upload requests include only the exact duplicate Codex record IDs; the server validates the prefix and deletes those IDs before upserting corrected records.
- Zero-token snapshots remain excluded. Cached input remains counted exactly once as part of Codex input usage, and reasoning remains counted exactly once as part of output usage.
- The production database has not been changed because deployment was not authorized in this request.

## Handoff

Deploy the committed source, run one all-history upload, verify the production daily totals, and move this requirement to `DONE`.
