# Cheap model integration and dialogue comparison implementation plan

> For agentic workers: use superpowers:executing-plans for integration and superpowers:dispatching-parallel-agents for the independent synthetic benchmark. Use test-first behavior changes and one independent final review.

**Goal:** Complete the ledger Agent with an inexpensive OpenAI model, measure it against the installed Qwen using history-like Chinese dialogue, and release the verified result.

**Architecture:** Keep authorized queries, deterministic calculations, scoped findings and the existing WebView. Add an explicit OpenAI Responses adapter with strict schemas and compatible parameters; retain native Ollama and generic compatible gateways. Record isolated per-turn call/token/cost metadata. The same synthetic fixtures and scoring execute against each provider.

**User constraints:** Begin with Luna; Terra or Sol only when measured failures justify them. Never use Astra. No real account data or credentials in Git or synthetic fixtures. Keep the existing stable member secret and production rollback path. User has authorized API use, Git integration and home deployment.

**Approaches:** The user selected inexpensive API models rather than the previously proposed flagship baseline. Reusing the existing generic JSON-only gateway is insufficient for strict contract probing. A separate OpenAI adapter preserves that gateway's existing behavior and adds Responses strict output without rewriting ledger tools.

**Review focus:** Missing schema defaults must remain fillable; incompatible temperature must not reach GPT-6; reasoning tokens and cache usage must be counted correctly; concurrent sessions must not share telemetry; benchmark budget stops must not become semantic failures; both models must see identical fixture data and scoring.

## Task 1: OpenAI adapter and usage accounting

- [ ] Add failing mock HTTP tests for Responses strict schema, model allowlist/configuration, no secret repr, compatible parameter omission and normalized plain JSON content.
- [ ] Implement explicit provider, schema normalization and bounded output budgets; preserve Ollama and existing compatible provider tests.
- [ ] Add per-turn isolated call/token/cost recording, including reasoning and cached input, without prompts or credentials; test concurrent isolation and error paths.
- [ ] Run the native contract probe against Luna and targeted end-to-end synthetic questions, recording actual usage.

## Task 2: Independent dialogue comparison

- [ ] Add 24–32 synthetic turns covering historical failure shapes and held-out wording, with independent expected calculations and context checks.
- [ ] Test scoring against incorrect queries, amounts, missing conditions and budget-stop behavior.
- [ ] Run the same committed code/fixtures with OpenAI and Qwen; collect individual outputs, task fulfillment, latency and API usage. Manually review substantive answers.
- [ ] Fix concrete failures with regression tests; rerun affected comparisons and report remaining limitations honestly.

## Task 3: Integration and release

- [ ] Complete backend/frontend checks and an independent review; update evaluation docs and Android README accurately.
- [ ] Perform authenticated private real-ledger acceptance on an isolated home service with the selected cheap model.
- [ ] Push and merge after checks, deploy the immutable merged revision with private environment and rollback; verify ready, assets, auth, history and enabled services.
- [ ] Sync the durable checkout and perform phone installation/WebView validation if a device is available.

**Ruling:** Preserve program-bound financial explanations while testing model changes. Better models do not make lost context, unauthorized records or unsupported money claims acceptable. Any later relaxation needs typed claims and evidence validation rather than a return to unrestricted financial prose.
