# Ledger analysis Agent redesign

The user authorized a complete implementation after reviewing failures in real conversations. Preserve the existing FastAPI/Vue/Android WebView architecture and authenticated ledger scope. The goal is useful, evidence-based analysis across income, spending, changes, forecasts, scenarios, savings, affordability and data quality, including follow-up questions, tables, charts and exports.

## Approach

Use a bounded investigation workflow: understand the question and conversation; query authorized records; compute evidence and source/member breakdowns; refine semantic source grouping using only identifiers from the observed catalog; render requested artifacts; explain and review the result. Arithmetic and artifact values belong to code. The language model selects tools, interprets evidence and states uncertainties. A larger model alone cannot recover discarded data; unrestricted model-generated arithmetic cannot establish ledger accuracy.

## Data and calculation contract

Keep remarks available as untrusted data. Convert authorized user IDs to non-sensitive stable member keys and display names; never send phone numbers or account identifiers to the model. Unknown membership remains explicitly unknown. Group historical records by member and observed source with traceable representative remarks. A `sources` analysis exposes bounded catalog rows for semantic grouping; source keys cannot be invented or used twice. Every omitted group stays represented under its observed label.

Income forecasts expose `sourceForecasts`: sourceKey, source, member, estimate, lower, upper, monthlyAmounts, remarks, and excluded/nonrecurring status. Calculate the headline estimate as the sum of included source estimates, rather than allocating an independently calculated total. Label absence as no recorded contribution in an observed month; wholly missing months stay unknown. Preserve exclusions and reference months. Future job stability is never inferred from a source name. User changes remain explicit assumptions.

## Output contract

Analysis artifacts are code-generated JSON objects: `id`, `kind` (`table`, `line`, `bar`), `title`, `columns` (objects with `key`, `label`, optional `unit`), `rows` (primitive-valued objects), optional `xKey`, optional `series` (objects with `key`, `label`), and `notes` (strings). Amounts must be finite, source-linked and reconcile. The model may reference artifact IDs but cannot create or edit their values. Backend Markdown tables keep non-browser clients useful. The Vue client renders native SVG charts and downloadable CSV/PNG without executing supplied HTML, SVG, scripts, URLs or formulas.

Persist artifacts with the assistant message. Return the same artifacts through SSE and authenticated history. History endpoints expose only the public artifact projection, never private evidence/debug metadata. Restored conversations must show the same chart and export as the original reply. Existing messages without artifacts remain supported.

## Conversation and reasoning

Include bounded user and assistant history, plus scoped structured state (time range, forecast target, requested output, conditions and source grouping). Old assistant text is conversational context, not fresh ledger evidence. Independent questions reset conditions; personal and family contexts cannot mix. Planner capabilities explicitly include remarks, member breakdowns, charts and exports; unsupported capabilities are stated accurately. A bounded refinement step can repair missing analysis tools or group observed sources, with schema checks and no arbitrary code or unrestricted network access.

Replace blanket numeric deletion with evidence-backed references: narrative numerical values use exact code-rendered value references, and tables/charts are deterministic. Review both factual support and task completion. On model failure, preserve useful relevant tables/charts and computed findings; do not substitute unrelated warnings. Keep model errors, validation, selected tools, fulfillment checks, version and duration available for later tuning without exposing raw identifiers.

## Scope and constraints

No new Android native implementation or ledger schema migration is needed. No ledger writes, transaction edits or historical-message rewrites. No credentials, real financial fixtures or conversation dumps in Git. Default to the installed Qwen model with bounded sequential inference and current GPU limits. This release supplies accurate financial charts; photographic illustration generation is a separate optional capability and must never fabricate financial charts.

## Acceptance

Replay the real failure shapes using synthetic equivalent records: per-source forecast table with reconciled total; remark follow-up with an actual query; family member/source attribution; six-month income line chart with missing months represented as unknown; cross-turn breakdown preserving target month. Cover spending, changes, anomalies, savings, purchase, protected expenses and scenarios as held-out questions. Test prompt-like remarks, unknown members, invalid source aliases, missing data, model timeouts and chart/CSV injection. Run all backend tests, frontend tests/build, an independent code review, local model acceptance and authenticated deployed readiness/history checks before reporting release success.
