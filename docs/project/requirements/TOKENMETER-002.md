---
schema_version: 1
id: TOKENMETER-002
project_id: TOKENMETER
title: Verify Codex usage accounting
status: DONE
priority: P1
executor: codex
task_id: local-audit-20260712
branch: main
worktree: local
dependencies: []
updated_at: '2026-07-12T10:56:11+08:00'
next_action: null
evidence:
- Independent 24-hour audit matched collector token fields for every retained event and found repeated cumulative snapshots.
- At the 2026-07-12 audit snapshot, production reported 2,716,949,772 Codex tokens for the day while independent deduplication removed 128,521,248 duplicated tokens, a 4.97 percent overstatement.
- Full local replay identified 6,223 duplicate record IDs.
- Temporary-database reconciliation produced exactly 42,288 Codex records and 6,357,548,806 tokens in both collector output and SQLite.
- All 21 unit and integration tests plus Python and installer syntax checks passed.
- Commit 87a8fe8 was deployed to tengxun after a SQLite backup and passed all 21 tests on the server.
- Full-history reconciliation produced exactly 44,486 local-Mac records and 6,744,306,410 tokens in both source rollout data and the production database at the same cutoff.
- The production database retained 63 independent tengxun-host Codex records, and both the public dashboard and dashboard API returned HTTP 200.
- A static 2026-07-12 snapshot produced 262,170,591 Codex tokens in opentoken but 3,146,328,583 under TokenMeter's current rule, a 12.00x overstatement.
- "The remaining defect is cross-thread fork/subagent history replay: child rollouts inherit parent token_count history, while the current collector only deduplicates inside each file."
- Forked rollout records are now removed, while root-thread events are reconciled to the local opentoken day/model totals without losing their Profile or interval timestamps.
- A frozen 1.1 GB Codex snapshot matched opentoken exactly across all 10 date/model buckets and every input, cache-read, cache-write, and output field.
- All 23 tests and canonical syntax checks passed locally and on tengxun before deployment.
- Production reconciliation uploaded 21,788 corrected records and removed 30,257 old fork or duplicate record IDs after a fresh SQLite backup.
- "Stable production dates matched opentoken exactly: 497,750,877 tokens on 2026-07-10 and 658,529,366 on 2026-07-11."
- The real 15-minute LaunchAgent completed with exit code 0, and the public dashboard API returned HTTP 200.
---

# Intent

Audit the Codex statistics end to end without reading or retaining prompt or response content. Verify that TokenMeter records incremental `token_count.last_token_usage` values once, assigns them to the event date, and preserves the same totals in SQLite.

## Acceptance criteria

- [x] Raw Codex usage events and collector output match by event identity and token fields.
- [x] Collector output removes inherited fork/subagent history across rollout files.
- [x] Daily Codex totals match an independent static opentoken aggregation.
- [x] Any scope limitation, uncertainty, or accounting defect is documented with reproducible evidence.

## Verification evidence

- The collector now skips `token_count` snapshots whose `total_token_usage.total_tokens` value is unchanged from the previous snapshot in the same rollout.
- Upload requests include only the exact duplicate Codex record IDs; the server validates the prefix and deletes those IDs before upserting corrected records.
- Zero-token snapshots remain excluded. Cached input remains counted exactly once as part of Codex input usage, and reasoning remains counted exactly once as part of output usage.
- The production database was backed up, reconciled by exact duplicate record ID, and verified against the source rollout data at the same timestamp.
- The earlier reconciliation proved database fidelity to the collector, not correctness of the collector's cross-thread accounting. Fork-aware reconciliation remains required.

## Handoff

Fork-aware reconciliation and deployment are complete. Continue monitoring the normal 15-minute upload while active Codex work adds new usage.
