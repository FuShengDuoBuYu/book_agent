# Analysis Quality Acceptance Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task by task; finish with a fresh whole-branch review.

**Goal:** Pass the eight user-approved analytical questions and the four-turn follow-up scenario with evidence-grounded, useful answers.

**Architecture:** Retain FastAPI, Vue, authorized ledger access, MongoDB and the home qwen3.5:9b model. Add missing decision tools and explicit carry-forward of user assumptions. For advanced analysis, generate a structured report using identified evidence statements, validate references/numbers, then review interpretations before sending the answer; an unsafe draft falls back to verified evidence and specific missing-input questions.

**Tech Stack:** Existing Python 3.11, Pydantic, LangChain/Ollama, unittest, Vue/Vite.

**Spec:** The user approved the eight questions and the follow-up chain in this conversation, and requested repeated diagnosis, fixes and retests. Existing deployment and Git push/merge authorization continues.

## Constraints and review focus

- Keep phone/family authorization, history pagination, SSE protocol and all existing supported calculations.
- Test synthetic ledgers with known ground truth first; never report selected tools alone as answer-quality success.
- A historical month must not become a future target; partial and missing months must not be treated as complete/zero.
- A question about additional savings does not require a starting balance; buying decisions and runway do.
- Source-specific percentage changes must apply to that source, persist across turns and never compound accidentally.
- Protect user-stated expense categories and preserve them when a deadline changes.
- Model prose must not introduce unsupported numeric claims or contradict evidence; unknown liquidity and future outcomes stay conditional.

## Tasks

### 1. Capture baseline and extend deterministic tools

Files: `scripts/evaluate_acceptance.py`, `app/tools/financial_analysis.py`, new `app/tools/decision_analysis.py`, `app/schemas/plan.py`, `tests/test_decision_analysis.py`.

- [x] Run the exact eight questions and follow-up chain against current production-equivalent code, retaining synthetic JSONL with plans/evidence/replies.
- [x] Add failing tests for three-month projection (12000), tutoring income reduced by half (9600), a five-month extension (16000), missing liquid balance, protected expenses and marked nonrecurring inflows.
- [x] Add projection, income stability, optimization, purchase, runway and data-quality tools; support signed percentage changes tied to an income/expense source. Missing decision inputs must remain explicit.
- [x] Run affected tests and commit the tool change.

### 2. Preserve analytical context

Files: `app/agent/planner.py`, new `app/agent/analysis_context.py`, `app/schemas/plan.py`, `app/agent/prompts.py`, `app/agent/book_agent.py`, `tests/test_analysis_context.py`.

- [x] Fail a deterministic four-turn test: projection -> source reduction -> protected categories -> deadline extension.
- [x] Carry forward structured assumptions on identified follow-ups; merge changed sources once, preserve constraints, and apply explicit month deltas. New independent questions and new conversations must not inherit stale assumptions.
- [x] Persist the resolved context with assistant metadata; use it for the next planner invocation.
- [x] Test changed scope, explicit reset and current facts overriding old conditions, then commit.

### 3. Validate analytical answers

Files: new `app/agent/evidence_report.py`, new `app/schemas/report.py`, `app/core/llm.py`, `app/agent/book_agent.py`, `tests/test_evidence_report.py`.

- [x] Fail tests for unknown fact references, unsupported numeric prose, unsupported affordability, and a failed critic check.
- [x] Build concise labeled evidence statements from tool results. A structured model report selects statements and explains their implications; render numeric statements from their verified source.
- [x] Check interpretations against evidence with a focused review call, retry once with concrete errors and otherwise return verified facts with relevant questions. Keep a heartbeat while generating and store validation metadata.
- [x] Keep simple queries and session/history contracts compatible; run the complete suite and frontend build.

### 4. Evaluate, review and publish

Files: `scripts/evaluate_acceptance.py`, `docs/analysis-quality-evaluation.md`, deployment release and Git PR.

- [x] Rerun the accepted questions and scenario variants; compare factual correctness, completeness, useful analysis, boundaries and continuity. Diagnose actual failures before another change.
- [x] Obtain a fresh review of the branch, fix concrete defects and rerun affected checks.
- [ ] Push and merge after CI, deploy an immutable release with rollback, verify ready/authentication/systemd, and sync the clean durable checkout.
- [ ] Report actual before/after findings and any remaining limitations, distinguishing model acceptance from deterministic tests.

## Execution ledger

- Initial root cause from current code: unchecked narrative is streamed directly; tool suite lacks liquidity/optimization/multi-month/source-percentage support; continuity relies on a truncated transcript rather than resolved state.
- Baseline running on home model with synthetic ledger, matching production provider/model/context/reasoning settings. Production remains unchanged while iterating.
- Reuse the clean, isolated development checkout on `codex/analysis-quality-acceptance`; no additional worktree needed.

Acceptance snapshot (2026-11-13): 109 deterministic/interface tests pass. Sixteen synthetic model cases are covered by the full batch plus affected-case retests; exact capture files and remaining limits are documented in docs/analysis-quality-evaluation.md. Release actions below are verified operationally after merge rather than claimed before publication.
