# TokenMeter Project Decisions

This file is append-only. Correct an earlier decision by adding a new superseding entry; do not rewrite historical entries.

## Entry template

### YYYY-MM-DD — Decision title

- Status: Proposed | Accepted | Superseded
- Context: Why a durable decision is needed.
- Decision: The selected rule or approach.
- Consequences: What future contributors and agents must do differently.
- Supersedes: None, or the title and date of the replaced decision.

## 2026-07-11 — Local Git files are the source of truth

- Status: Accepted
- Context: Codex and ZCode tasks and chats are useful execution surfaces but can drift, be archived, or omit verification state.
- Decision: Version-controlled local Git files hold TokenMeter requirements, status, decisions, handoffs, and verification evidence. Chat and task metadata may link to these files but does not replace them.
- Consequences: Agents must update the active requirement at workflow boundaries and may mark work `DONE` only when acceptance criteria and durable evidence are present.
- Supersedes: None.
