# Conclusion-first ledger answers

## Intent and observed failure

The user wants judgments, synthesis and useful next steps, supported by a few relevant numbers. They rejected data dumps, excessive length and a model restricted to selecting an outline. Earlier instructions authorize implementing Agent fixes autonomously and preserve the existing home module layout.

Two private production messages reproduced the failure: a six-month change question produced16672 characters and five tables; a one-number income forecast produced4876 characters and two tables although output.table=false. Both had correct understanding and a successful outline, not leaked thinking or a failed model call. The report layer required evidence prose to be displayed, permitted no model synthesis and appended every artifact as Markdown. Artifacts also rendered expanded in the UI.

## Approach

A display-only collapse would conceal the symptom while leaving template-only reasoning. Unrestricted financial prose would reintroduce invented quantities and unsafe conclusions. The implementation uses one structured, evidence-bound conclusion composer instead of the outline-only call: conclusion, few reasons, optional next step and a relevant caveat. The model can synthesize qualitative implications, while numeric/calendar/name references resolve from exact cited facts. Evidence ownership, scopes, known missing decision inputs and computed decision states are checked in code; semantic correctness is additionally judged by independent full-answer review.

## Constraints and acceptance

- Keep the existing authorized tools, source investigation and deterministic financial calculations. Keep Terra, current API privacy settings and no external traces. No new dependencies or flagship model calls.
- Normal answers lead with a direct judgment/estimate/explicit inability to decide, usually fewer than700 Chinese characters and at most1200 rendered characters. Detailed requests may use a larger bounded budget. A length cap alone is not success.
- Numeric/date claims use typed value references owned by their cited facts. Literal note identifiers are inert data. Reject unknown references, fabricated numbers and unsupported positive decisions when required balance/price/reserve inputs are missing. Do not turn transfers into earned income, recorded outflow into consumption, ledger net into available balance or correlation into proven cause.
- All evidence and calculation results remain in private metadata/history. Stop requiring every audit fact in visible prose. Keep critical conditional/missing-input boundaries and primary decision answers.
- Do not append artifact Markdown to normal assistant content. Keep full authorized artifacts for expansion/download/history; supporting artifacts are collapsed, explicitly requested tables/charts can be shown. Existing long history must not create duplicate table displays.
- Time windows and household/member scopes remain visible in evidence; do not silently combine different windows or borrow balances across books. If source evidence cannot justify a cause, say what changed and what needs verification.
- Safe fallback stays concise and explicitly bounded; never dump all facts on timeout/validation failure. Preserve the private failure reason and full evidence for tuning.
- No real amounts, names, identifiers or conversations in Git. Reproduction fixtures are fictional. Real message replays stay under /root/runtime/state/book-agent/evaluations; candidates under the module cache, secrets under existing secrets. Never create root-level/opt deployment trees.

## Validation

Test-first regressions cover direct forecast, multi-month interpretation, unknown purchase feasibility, negative/positive computed decisions, protected categories, scope/window borrowing, numeric/name injection, source text and long evidence, no-table versus explicit table and history restoration. Run the full Python and frontend suites/build, real-model synthetic dialogues and a private replay of the two actual failed outputs. Review whether each answer addresses the question, explains implications with correctly scoped support and avoids unrelated detail. Then stage with actual namespace protection, public/auth/history checks, Git PR/CI and controlled release/rollback using canonical infra paths.

## Independent review rulings

Computed true/false/unknown financial predicates remain immutable judgment references. Full audit facts, bounded composer context and essential visible conditions are separate. Assign references before trimming/repair and retain fact, role, unit and scope/window privately. Check final combined prose length without truncating a condition. Fix semantic gold before model calls; both actual failures must pass at one final revision.
