---
schema_version: 1
id: TOKENMETER-003
project_id: TOKENMETER
title: 精简看板顶部统计卡片
status: DONE
priority: P1
executor: codex
task_id: codex-tokenmeter-dashboard-metric-review-20260712
branch: main
worktree: local
dependencies:
- TOKENMETER-002
updated_at: '2026-07-12T16:43:50+08:00'
next_action: null
evidence:
- The current diff removes the active-days card and updates the dashboard documentation to match.
- All 23 tests, Python compilation, installer shell syntax, and git diff checks passed locally.
- The same 23 tests and syntax checks passed in the isolated tengxun staging directory before deployment.
- tokenmeter.service restarted successfully while retaining /var/lib/tokenmeter/tokenmeter.sqlite, and
  the public /tokenmeter route returned HTTP 200 with exactly two metric cards and no active-days text.
- 'closeout: TOKENMETER passed clean and synchronized with origin/main through 4e030fa'
---

# Intent

Track the current uncommitted dashboard simplification separately from the completed Codex accounting audit.

## Acceptance criteria

- [x] The active-days card and its unused JavaScript calculation are removed together.
- [x] The metric grid and README describe the remaining token and cost cards.
- [x] Canonical TokenMeter validation passes.
- [x] The intended source and governance files are reviewed as one commit boundary.
- [x] Push occurs only with explicit authorization.

## Verification plan

- Run the three canonical TokenMeter validation commands.
- Run `git diff --check` and inspect the intended file list.
