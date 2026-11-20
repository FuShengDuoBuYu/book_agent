"""Bounded model context for planning and evidence selection."""

import json

from app.agent.report_values import value_catalog


CHAT_RESERVE = 256
EVIDENCE_SELECTION_RESERVE = 512


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _text_tokens(text):
    wide = sum(not char.isascii() for char in text)
    return (9 * wide + 2 * (len(text) - wide) + 5) // 6


def _prompt_budget(system_text, schema, num_ctx, num_predict):
    schema_json = schema if isinstance(schema, dict) else schema.model_json_schema()
    fixed = _text_tokens(system_text) + _text_tokens(_compact_json(schema_json))

    def estimate(payload):
        return fixed + _text_tokens(_compact_json(payload))

    return num_ctx - num_predict - CHAT_RESERVE, estimate


def _largest_fit(count, fits):
    low, high = 0, count
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def _artifact_previews(artifacts, row_limit=None):
    return [{**artifact, 'rows': artifact.get('rows', [])[:row_limit],
             'totalRows': artifact.get('totalRows', len(artifact.get('rows', [])))}
            for artifact in artifacts]


def _fit_previews(payload, estimate, limit):
    if estimate(payload) <= limit:
        return payload

    artifacts = payload.get('artifacts', [])
    if artifacts:
        row_limit = _largest_fit(max(len(item['rows']) for item in artifacts),
                                 lambda n: estimate({**payload, 'artifacts': _artifact_previews(artifacts, n)}) <= limit)
        payload = {**payload, 'artifacts': _artifact_previews(artifacts, row_limit)}
    history = payload.get('history', '')
    if history and estimate(payload) > limit:
        chars = _largest_fit(len(history), lambda n: estimate({**payload, 'history': history[-n:]}) <= limit)
        payload = {**payload, 'history': history[-chars:] if chars else ''}
    return payload


def _with_evidence(payload, facts, *, include_values):
    projected = {
        **payload,
        'facts': [{'id': row['id'], 'text': row.get('modelText') or row['text'],
                   'required': bool(row.get('required')), **({'binding': row['binding']} if row.get('binding') else {})} for row in facts],
    }
    if include_values:
        projected['values'] = {row['id']: f"{row['factId']} {row['label']} = {row['display']}"
                               for row in value_catalog(facts)}
    else:
        projected.pop('values', None)
    return projected


def _ensure_fit(payload, estimate, limit):
    tokens = estimate(payload)
    if tokens > limit:
        raise ValueError(f'模型必需上下文超出预算（估算 {tokens}，上限 {limit}）')
    return tokens


def _stats(payload, estimate, limit):
    facts = payload.get('facts', [])
    return {
        'estimatedPromptTokens': estimate(payload),
        'promptBudgetTokens': limit,
        'requiredFactCount': sum(bool(row.get('required')) for row in facts),
        'selectedFactCount': len(facts),
        'artifactPreviewRows': sum(len(item['rows']) for item in payload.get('artifacts', [])),
        'historyChars': len(payload.get('history', '')),
    }


def build_report_context(
    base_payload, facts, system_text, schema, *, num_ctx=8192, num_predict=2048, include_values=True,
):
    """Preserve the goal and required evidence while selecting previews within the prompt budget."""
    limit, estimate = _prompt_budget(system_text, schema, num_ctx, num_predict)
    required = [row for row in facts if row.get('required')]
    optional = [row for row in facts if not row.get('required')]
    previews = _artifact_previews(base_payload.get('artifacts', []))
    base = {**base_payload, 'artifacts': previews}
    minimum = {**base, 'history': '', 'artifacts': _artifact_previews(previews, 0)}
    required_tokens = _ensure_fit(_with_evidence(minimum, required, include_values=include_values), estimate, limit)
    draft_limit = max(required_tokens, limit - min(EVIDENCE_SELECTION_RESERVE, limit // 8))

    selected = required + optional
    payload = _fit_previews(_with_evidence(base, selected, include_values=include_values), estimate, draft_limit)
    if estimate(payload) > draft_limit:
        count = _largest_fit(
            len(optional),
            lambda n: estimate(_with_evidence(minimum, required + optional[:n], include_values=include_values))
            <= draft_limit,
        )
        selected = required + optional[:count]
        # Reclaim available space for recent turns or previews once optional evidence is dropped.
        payload = _fit_previews(_with_evidence(base, selected, include_values=include_values), estimate, draft_limit)

    _ensure_fit(payload, estimate, limit)
    stats = _stats(payload, estimate, limit)
    stats['omittedOptionalFactCount'] = len(facts) - len(selected)
    return payload, selected, stats


def _fit_evidence(payload, estimate, limit):
    reduced = _fit_previews(payload, estimate, limit)
    if estimate(reduced) <= limit:
        return reduced

    facts = payload.get('facts', [])
    required = {row['id'] for row in facts if row.get('required')}
    optional = [row['id'] for row in facts if not row.get('required')]

    def retain(count, base):
        identifiers = required.union(optional[:count])
        result = {**base, 'facts': [row for row in facts if row['id'] in identifiers]}
        if 'values' in base:
            # Value IDs identify typed facts outside this evidence projection.
            result['values'] = {key: display for key, display in base['values'].items()
                                if display.split(' ', 1)[0] in identifiers}
        return result

    count = _largest_fit(len(optional), lambda n: estimate(retain(n, reduced)) <= limit)
    return _fit_previews(retain(count, payload), estimate, limit)
