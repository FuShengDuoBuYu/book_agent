# Ledger analysis Agent redesign

The user authorized a complete implementation after reviewing failures in real conversations. Preserve the existing FastAPI/Vue/Android WebView architecture and authenticated ledger scope. The goal is useful, evidence-based analysis across income, spending, changes, forecasts, scenarios, savings, affordability and data quality, including follow-up questions, tables, charts and exports.

## Approach

Use a bounded investigation workflow: understand the question and conversation; query authorized records; compute evidence and source/member breakdowns; refine semantic source grouping using only identifiers from the observed catalog; render requested artifacts; select a scoped outline and render computed findings. Arithmetic and artifact values belong to code. The language model interprets the user goal, selects tools and evidence, and investigates observed sources. Code renders financial judgments and uncertainties derived from calculated evidence. A larger model alone cannot recover discarded data; unrestricted model-generated arithmetic cannot establish ledger accuracy.

## Data and calculation contract

Keep remarks available as untrusted data. Convert authorized user IDs to non-sensitive stable member keys and display names; never send phone numbers or account identifiers to the model. Unknown membership remains explicitly unknown. Group historical records by member and observed source with traceable representative remarks. A `sources` analysis exposes bounded catalog rows for semantic grouping; source keys cannot be invented or used twice. Every omitted group stays represented under its observed label.

Income forecasts expose `sourceForecasts`: sourceKey, source, member, estimate, lower, upper, monthlyAmounts, remarks, and excluded/nonrecurring status. Calculate the headline estimate as the sum of included source estimates, rather than allocating an independently calculated total. Label absence as no recorded contribution in an observed month; wholly missing months stay unknown. Preserve exclusions and reference months. Future job stability is never inferred from a source name. User changes remain explicit assumptions.

## Output contract

Analysis artifacts are code-generated JSON objects: `id`, `kind` (`table`, `line`, `bar`), `title`, `columns` (objects with `key`, `label`, optional `unit`), `rows` (primitive-valued objects), optional `xKey`, optional `series` (objects with `key`, `label`), and `notes` (strings). Amounts must be finite, source-linked and reconcile. The model may reference artifact IDs but cannot create or edit their values. Backend Markdown tables keep non-browser clients useful. The Vue client renders native SVG charts and downloadable CSV/PNG without executing supplied HTML, SVG, scripts, URLs or formulas.

Persist artifacts with the assistant message. Return the same artifacts through SSE and authenticated history. History endpoints expose only the public artifact projection, never private evidence/debug metadata. Restored conversations must show the same chart and export as the original reply. Existing messages without artifacts remain supported.

## Conversation and reasoning

Include bounded user and assistant history, plus scoped structured state (time range, forecast target, requested output, conditions and source grouping). Old assistant text is conversational context, not fresh ledger evidence. Independent questions reset conditions; personal and family contexts cannot mix. Planner capabilities explicitly include remarks, member breakdowns, charts and exports; unsupported capabilities are stated accurately. A bounded refinement step can repair missing analysis tools or group observed sources, with schema checks and no arbitrary code or unrestricted network access.

Keep exact code-rendered facts and deterministic tables/charts. The model selects a bounded neutral outline bound to the current user's answer goals and scoped fact IDs; it cannot supply financial prose. Code renders observations, limitations and actions derived from each bound calculation group. Required evidence omitted by the outline is supplemented with its own computed interpretation. Never substitute old assistant amounts for user inputs.

On outline failure, preserve relevant facts, computed interpretation and requested artifacts immediately. Keep errors, selected goals/tools, scope, calculation findings, call timing, analysis version and release SHA in private metadata for tuning; public history receives messages and validated artifacts only.

## Scope and constraints

No new Android native implementation or ledger schema migration is needed. No ledger writes, transaction edits or historical-message rewrites. No credentials, real financial fixtures or conversation dumps in Git. Default to the installed Qwen model with bounded sequential inference and current GPU limits. This release supplies accurate financial charts; photographic illustration generation is a separate optional capability and must never fabricate financial charts.

## Acceptance

Replay the real failure shapes using synthetic equivalent records: per-source forecast table with reconciled total; remark follow-up with an actual query; family member/source attribution; six-month income line chart with missing months represented as unknown; cross-turn breakdown preserving target month. Cover spending, changes, anomalies, savings, purchase, protected expenses and scenarios as held-out questions. Test prompt-like remarks, unknown members, invalid source aliases, missing data, model timeouts and chart/CSV injection. Run all backend tests, frontend tests/build, an independent code review, local model acceptance and authenticated deployed readiness/history checks before reporting release success.

## Computed report composition (2026-11-19 refinement)

Actual-model replay exposed correct numbers paired with wrong financial interpretation: historical expense changes were presented as applied future assumptions; protected categories became suggested cuts; unchanged monthly earning capacity was confused with unchanged target pressure. Model self-review missed substantive errors. A model verdict is therefore not the grounding gate.

Use one ReportOutline call to select only current user answer goals, neutral titles and fact IDs. Code validates references and decision headline dependencies, preserves all mandatory evidence, and renders analysisFindings observations, limitations and actions. Every finding is bound to the same result group, tool and ledger scope as its facts; each group is interpreted once. User-controlled source text is inert Markdown data, including strings resembling quantity variables. No ReportComment, free financial writer, quantity substitution, regex prose repair or final model critic remains.

This does not replace intent understanding with keyword extraction. IntentResolver, scoped conversation state, planning validation, source investigation and analytical tools continue to determine what must be answered. The release must evaluate wrong/missing tools, stale conditions, invented inputs and lost referents separately from report grounding. A correct computed report for the wrong question is still a failed answer.

Private metadata uses analysisVersion=5, validation.status=grounded and compositionStatus=planned/fallback. Grounded means code consistency, not semantic acceptance. Capture releaseRevision and actual model settings so later tuning can reproduce a failure. Run configured-model replay and independent qualitative review before promotion.
