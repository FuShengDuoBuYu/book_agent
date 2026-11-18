"""Bounded model context for the evidence-backed report writer."""

import json

from app.agent.report_values import value_catalog


CHAT_RESERVE = 256
MAX_REPAIR_RESERVE = 512
MAX_PREVIOUS_SECTIONS = 6
MAX_PREVIOUS_ANALYSIS_CHARS = 600
MAX_PREVIOUS_TEXT_CHARS = 1200


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
    draft_limit = max(required_tokens, limit - min(MAX_REPAIR_RESERVE, limit // 8))

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


def _excerpt(text, length):
    return text if len(text) <= length else text[:max(0, length - 1)] + '…'


def _fit_repair_evidence(payload, estimate, limit):
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
            # Writer value IDs identify typed facts outside this projection.
            result['values'] = {key: display for key, display in base['values'].items()
                                if display.split(' ', 1)[0] in identifiers}
        return result

    count = _largest_fit(len(optional), lambda n: estimate(retain(n, reduced)) <= limit)
    return _fit_previews(retain(count, payload), estimate, limit)


def _fit_previous(payload, report, estimate, limit, *, outline_only=False):
    def fits(value):
        return estimate({**payload, 'previousReport': value}) <= limit

    if outline_only and isinstance(report, str):
        return None
    if isinstance(report, str):
        chars = _largest_fit(min(len(report), MAX_PREVIOUS_TEXT_CHARS), lambda n: fits(_excerpt(report, n)))
        return _excerpt(report, chars) if chars else None
    if not isinstance(report, dict):
        return None

    allowed = {row['id'] for row in payload.get('facts', [])}

    def variant(sections, chars):
        rows = []
        for section in sections:
            row = {'title': _excerpt(section['title'], min(chars, 120)),
                   'factIds': [key for key in section['factIds'][:8] if key in allowed]}
            if not outline_only:
                row['analysis'] = _excerpt(section['analysis'], chars)
            rows.append(row)
        return {'sections': rows}

    sections = report.get('sections', [])[:MAX_PREVIOUS_SECTIONS]
    count = _largest_fit(len(sections), lambda n: fits(variant(sections[:n], 1)))
    if not count:
        return None
    sections = sections[:count]
    chars = _largest_fit(MAX_PREVIOUS_ANALYSIS_CHARS, lambda n: fits(variant(sections, n)))
    return variant(sections, chars)


def repair_context(payload, previous_report, issues, system_text, schema, *, num_ctx=8192, num_predict=2048):
    """Fit complete diagnostics first and keep required evidence and reference IDs intact."""
    limit, estimate = _prompt_budget(system_text, schema, num_ctx, num_predict)
    corrections = list(dict.fromkeys(issue for issue in map(str, issues) if issue.strip()))
    repaired = _fit_repair_evidence({**payload, 'previousReport': None, 'corrections': corrections}, estimate, limit)
    _ensure_fit(repaired, estimate, limit)
    repaired['previousReport'] = _fit_previous(
        repaired, previous_report, estimate, limit, outline_only=bool(corrections),
    )
    _ensure_fit(repaired, estimate, limit)
    stats = _stats(repaired, estimate, limit)
    stats['correctionCount'] = len(corrections)
    stats['omittedOptionalFactCount'] = len(payload.get('facts', [])) - len(repaired.get('facts', []))
    return repaired, stats
