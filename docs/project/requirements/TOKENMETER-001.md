---
schema_version: 1
id: TOKENMETER-001
project_id: TOKENMETER
title: Adopt unified AI project governance
status: DONE
priority: P1
executor: codex
task_id: 019f502b-19fb-7ab3-b48b-3187b9d808b4
branch: main
worktree: local
dependencies: []
updated_at: '2026-07-11T18:48:33+08:00'
next_action: null
evidence:
- Created project control files
- 'AI-Portfolio check: 0 errors and 0 warnings; generated dashboard and project status verified on 2026-07-11.'
---

# Intent

Bring TokenMeter under the shared Codex and ZCode project-control protocol while preserving the repository's existing product behavior and local host-audit compatibility workflow.

## Acceptance criteria

- [x] TokenMeter's repository contract and canonical validation commands are documented.
- [x] Project metadata and a review-ready requirement record exist in `docs/project/`.
- [x] The durable source-of-truth decision is recorded in the append-only decision log.
- [x] AI-Portfolio check succeeds

## Verification evidence

- Created the project control files without modifying TokenMeter production code.
- `PYTHONPATH=src python3 -m unittest discover -s tests` passed 18 tests; Python compilation and installer shell syntax checks also passed.
- Repository validation and the cross-project portfolio check are recorded separately; the portfolio check remains the review gate.

## Handoff

Run `AI-Portfolio` validation against this repository, render the portfolio dashboard, attach the result as evidence, and move this requirement to `DONE` only when all acceptance criteria pass.
