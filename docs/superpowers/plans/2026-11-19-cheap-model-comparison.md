# Cheap model integration and dialogue comparison implementation plan

> For agentic workers: use superpowers:executing-plans for integration and superpowers:dispatching-parallel-agents for the independent synthetic benchmark. Use test-first behavior changes and one independent final review.

**Goal:** Complete the ledger Agent with an inexpensive OpenAI model, measure it against the installed Qwen using history-like Chinese dialogue, and release the verified result.

**Architecture:** Keep authorized queries, deterministic calculations, scoped findings and the existing WebView. Add an explicit OpenAI Responses adapter with strict schemas and compatible parameters; retain native Ollama and generic compatible gateways. Record isolated per-turn call/token/cost metadata. The same synthetic fixtures and scoring execute against each provider.

**User constraints:** Begin with Luna; Terra or Sol only when measured failures justify them. Never use Astra. No real account data or credentials in Git or synthetic fixtures. Keep the existing stable member secret and production rollback path. User has authorized API use, Git integration and home deployment.

**Approaches:** The user selected inexpensive API models rather than the previously proposed flagship baseline. Reusing the existing generic JSON-only gateway is insufficient for strict contract probing. A separate OpenAI adapter preserves that gateway's existing behavior and adds Responses strict output without rewriting ledger tools.

**Review focus:** Missing schema defaults must remain fillable; incompatible temperature must not reach GPT-6; reasoning tokens and cache usage must be counted correctly; concurrent sessions must not share telemetry; benchmark budget stops must not become semantic failures; both models must see identical fixture data and scoring.

## Task 1: OpenAI adapter and usage accounting

- [x] Add failing mock HTTP tests for Responses strict schema, model allowlist/configuration, no secret repr, compatible parameter omission and normalized plain JSON content.
- [x] Implement explicit provider, schema normalization and bounded output budgets; preserve Ollama and existing compatible provider tests.
- [x] Add per-turn isolated call/token/cost recording, including reasoning and cached input, without prompts or credentials; test concurrent isolation and error paths.
- [x] Run the native contract probe against Luna and targeted end-to-end synthetic questions, recording actual usage.

## Task 2: Independent dialogue comparison

- [x] Add 24–32 synthetic turns covering historical failure shapes and held-out wording, with independent expected calculations and context checks.
- [x] Test scoring against incorrect queries, amounts, missing conditions and budget-stop behavior.
- [x] Run the same committed code/fixtures with OpenAI and Qwen; collect individual outputs, task fulfillment, latency and API usage. Manually review substantive answers.
- [x] Fix concrete failures with regression tests; rerun affected comparisons and report remaining limitations honestly.

## Task 3: Integration and release

- [x] Complete backend/frontend checks and an independent review; update evaluation docs and Android README accurately.
- [x] Perform authenticated private real-ledger acceptance on an isolated home service with the selected cheap model.
- [ ] Push and merge after checks, deploy the immutable merged revision with private environment and rollback; verify ready, assets, auth, history and enabled services.
- [ ] Sync the durable checkout and perform phone installation/WebView validation if a device is available.

**Ruling:** Preserve program-bound financial explanations while testing model changes. Better models do not make lost context, unauthorized records or unsupported money claims acceptable. Any later relaxation needs typed claims and evidence validation rather than a return to unrestricted financial prose.


## Actual comparison findings and bounded follow-up

- Frozen initial application `f8db882` completed Qwen and Luna thirty-turn runs; Terra uses that same application and fixture for its comparison. Scorer-only revisions are applied uniformly to saved raw outputs, with no feedback to model generation.
- Confirmed and regression-tested: source-group fragments dropping a stage; unverified planner groups blocking confirmed follow-up grouping; omitted negative anomaly results; failed turns losing history/usage. Correct historical samples and equivalent nonoverlapping bank tables now have independent checks.
- Two shared failures require program corrections before model selection: colloquial Chinese amount literals were rejected by the guard, and an explicitly removed deadline was reintroduced after the model review correctly set it to null. Add test-first fixes, then rerun the affected dialogues with the compared providers.
- Production is still the original release until authenticated staged acceptance and final merge succeed. USB devices were not connected at the latest ADB check.


## Nov20 completion evidence

- Frozen `199e1e9` three-provider 30-turn runs completed with identical fixture/date/questions. Independent anonymized full-answer review: Qwen21, Luna22, Terra25. The stricter manual score differs from auto26/25/28; equivalent-unit answers and raw-catalog redundancy were adjudicated uniformly.
- Default selected Terra. Known call estimate subtotals and unknown-cost turns are recorded separately in `docs/model-comparison-2026-11-19.md`; neither reserve allowances nor missing usage are presented as actual charges. Sol/Astra were not called.
- `f579c31` test-first corrections bind income exclusions/denominators/windows, resolve an explicit no-deadline savings continuation, collapse only equivalent normalized goals, and preserve verified deadline comparisons without exposing internal snapshots as user conditions. 527 backend tests passed on home Python3.13 and GitHub Python3.11; CI includes frontend checks. Independent review found no remaining blockers in this bounded patch.
- `f579c31` + Terra private real-ledger acceptance passed all35 checks: two normal turns, exact category comparisons, filtered category table, history/artifact restoration, secure auth, no raw private metadata, stable ledger snapshot and temporary-grant cleanup. Full private report remains only on the home host. An initial check started before the stage listener was ready failed connection; it created no AI answer, then the ready200 rerun completed.
- Three later 9-turn targeted runs are separate from the frozen full matrix. They are not combined into a claimed final 30-turn score.
- Nov20 ADB inventory is empty; current APK installation and phone WebView testing remain conditional on an attached authorized device. No native change was needed for the server-model selection.

- Bounded 9-turn reruns completed: manual Qwen6/9, Luna9/9, Terra9/9; remaining Qwen planning failures and an automatic-scoring false negative are documented separately.

- Required `evaluate_intent --case expense_decision` on f579c31+Terra completed three normal turns with all checks passing; raw outputs are separate from the comparison matrix.
