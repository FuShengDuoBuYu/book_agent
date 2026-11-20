# Conclusion-first report repair implementation plan

> For agentic workers: use superpowers:executing-plans for native implementation and superpowers:requesting-code-review for an independent whole-change review. User has authorized code fixes, Git integration and deployment; current turn specifies conclusion-first behavior.

**Goal:** Replace outline/data-dump responses with concise evidence-bound conclusions and useful analysis.
**Architecture:** One structured composer chooses conclusion/reasons/next step/caveat and typed cited values; programs retain calculations, validate evidence boundaries and render. Full supporting artifacts remain separate and expandable.
**Tech Stack:** Existing FastAPI/Pydantic, OpenAI Responses/Terra, Vue/Vite, unittest and Node tests; no new dependencies.
**Spec:** docs/superpowers/specs/2026-11-20-conclusion-first-design.md

## Global constraints

Reuse canonical home apps/runtime/secrets/infra layout and current models/privacy. No real ledger data in Git. Preserve authorization, evidence, history and download behavior. Default prose bounded1200 characters, aim under500; detailed requests bounded separately. One normal composition call replaces outline; a bounded repair may precede concise safe fallback.

## Review focus

- Correct-looking numbers cited from the wrong source/book/window.
- A conditional forecast expressed as a guaranteed income or balance.
- Missing purchase/goal inputs converted into a positive or negative definitive decision.
- Verbose fallback and automatic source/remark tables overwhelming a simple question.
- Explicit table/detail requests lost when changing default prose and artifact display.

### Task1: Evidence-bound conclusion composition

Files: app/schemas/report.py, app/agent/report_composition.py, app/agent/evidence_report.py, app/agent/report_context.py as needed; tests/test_report_composition.py, tests/test_report_v2.py and focused new conclusion regressions.
- [x] Write failing regressions for a direct synthesized conclusion, typed reference ownership, fabricated quantities, missing decision inputs and concise fallback.
- [x] Run regressions to observe the intended failures before implementation.
- [x] Replace outline-only schema/composition, retire unused outline code, retain reusable scoped findings and value helpers. Record raw structured draft, reference validation and bounded failure/repair metadata privately.
- [x] Verify focused tests and migrate old tests whose required data-dump contract is intentionally replaced, preserving safety assertions.

### Task2: Supporting data display

Files: app/agent/book_agent.py, app/agent/artifacts.py, app/schemas/artifacts.py, frontend/src/artifacts.js, frontend/src/AnalysisArtifacts.vue; existing Python/Node artifact/history tests.
- [x] Add failing no-Markdown-append and collapsed-supporting-versus-requested artifact regressions.
- [x] Keep data separate, preserve public artifact validation and fold supporting evidence by default while honoring explicit display requests.
- [ ] Verify history/export, literal content and mobile UI behavior.

### Task3: Actual answer quality and release

Files: synthetic conclusion evaluation script/fixtures, relevant architecture/evaluation/release docs; private replays only on home module state.
- [ ] Run full backend suite, frontend tests/build and independent code review.
- [ ] Run history-like synthetic model questions and private saved-evidence replays of both actual failures; read full replies and record failures/costs separately from structural checks.
- [ ] Fix observed failures with regression tests and rerun affected cases; do not claim a final full matrix from mixed revisions.
- [ ] Push PR, wait CI, merge, stage/release through canonical infra paths with private rollback and real authorized public/history acceptance. Update deployed revision and source checkout; retain only purposeful module artifacts.
