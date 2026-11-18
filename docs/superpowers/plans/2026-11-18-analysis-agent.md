# Ledger Analysis Agent Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for integration and superpowers:dispatching-parallel-agents for the independent data and presentation components. Each component follows test-first development and the whole branch receives independent review.

**Goal:** Fulfill real analytical questions and follow-ups with traceable calculations, useful explanations, tables, charts and exports.

**Architecture:** Bounded planner/investigation and deterministic ledger tools produce scoped evidence and artifacts. A reference-based report explains those values; persisted artifacts survive history reloads.

**Tech Stack:** Existing Python/FastAPI/Pydantic/unittest, Vue/Vite, native SVG and Node test runner.

**Spec:** `docs/superpowers/specs/2026-11-18-analysis-agent-design.md`

## Global Constraints

- Preserve authentication, personal/family isolation and existing Android WebView.
- No ledger writes, real ledger fixtures, credentials or private conversation dumps in Git.
- Artifacts follow the exact JSON contract in the spec; model output cannot change artifact data.
- Bound source grouping, history size, refinement rounds and model calls.

## Review Focus

- Source medians must add to the headline forecast; excluded income stays auditable.
- A repeated source label across members must not erase member attribution.
- A follow-up inherits target and scope, never unrelated balances or another family's state.
- A failed model still returns relevant requested artifacts and accurate known findings.
- History artifacts and CSV/chart downloads must resist data/HTML/formula injection.

### Task 1: Authorized source and member evidence

**Files:** `app/tools/order_search.py`, `app/tools/source_analysis.py`, `app/tools/financial_analysis.py`, associated tests.

**Interfaces:** `run_analysis('sources', ...)` yields observed source catalog; forecast yields additive `sourceForecasts`. Records carry `memberKey` and `memberName`, never raw user IDs. Optional `source_groups` uses existing catalog keys only.

- [ ] Add failing tests for safe member attribution, remark evidence, grouped source forecasts and reconciliation, missing months and exclusions.
- [ ] Run new tests RED, implement data tools, run them GREEN and existing calculation/decision tests.

### Task 2: Structured artifact presentation

**Files:** `frontend/src/AnalysisArtifacts.vue`, `frontend/src/artifacts.js`, `frontend/src/App.vue`, frontend tests/package scripts, styles.

**Interfaces:** Receives artifacts from SSE `{type:'artifacts', artifacts:[...]}` and history `message.artifacts`; renders table/line/bar using the spec contract. Exports CSV and PNG from local validated data.

- [ ] Add failing tests for exact chart series, unknown gaps, formula escaping and malformed artifacts.
- [ ] Implement validated SVG/table rendering, history hydration and downloads; run Node tests and Vite build.

### Task 3: Investigation, conversation and reference-backed reports

**Files:** planner/schema/prompts, `app/agent/book_agent.py`, report/evidence/artifact modules, memory history projection, integration tests.

**Interfaces:** Preserve existing financial tools, add source-aware investigation; reporter accepts history, final instructions and deterministic artifacts. Store public artifacts in assistant metadata and expose their validated projection in history.

- [ ] Add failing synthetic historical replay tests and numeric-reference integrity tests.
- [ ] Implement bounded refinement and scoped history/state; replace quantity stripping with reference-backed narrative and deterministic relevant fallback.
- [ ] Persist and stream artifacts; test authenticated history and all suites.

### Task 4: Quality acceptance and release

**Files:** acceptance runner/fixtures, architecture and evaluation docs, README, CI.

- [ ] Run held-out local-model questions across the supported analysis tools and replay the historical failure shapes without writing synthetic messages into production history.
- [ ] Independent review; fix material findings with regression tests.
- [ ] Commit, push and integrate through GitHub checks under the user's existing authorization.
- [ ] Deploy an immutable release with rollback, validate readiness and authenticated real-ledger outputs, and document measured limitations.
