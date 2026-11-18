"""Compact conversation conditions supplied as data to a planning model."""

from copy import deepcopy

from app.agent.analysis_context import FIELDS


TASK_CONDITIONS = (*FIELDS, 'targetMonth', 'budgetAmount', 'changes', 'protectedCategories')


def _copy_without_defaults(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            cleaned = _copy_without_defaults(item)
            if cleaned is None or cleaned == '' or cleaned == [] or cleaned == {}:
                continue
            result[key] = cleaned
        return result
    if isinstance(value, list):
        # Scope triples are positional, including the empty remark filter.
        return [_copy_without_defaults(item) for item in value]
    return deepcopy(value)


def _logical_queries(queries, *, has_window):
    result = []
    for query in queries:
        original_args = query.get('args') or {}
        args = {'cost_type': original_args.get('cost_type', '不限'), 'remark': original_args.get('remark', '')}
        bindings = query.get('sourceIds')
        if not bindings and not has_window:
            # Legacy logical lookups may carry the only explicit date for an older turn.
            args.update({key: original_args[key] for key in ('year', 'month', 'day')
                         if original_args.get(key) is not None})
        for identifier in bindings or [query['id']]:
            logical = {'id': identifier, 'toolName': query['toolName'], 'args': args.copy()}
            if logical not in result:
                result.append(logical)
    return result


def _remove_duplicate_conditions(tasks, context, queries):
    scopes = context.get('scope', [])
    if context.get('ambiguous') or context.get('scenarios') or len(scopes) != 1:
        return
    for task in tasks:
        identifiers = set(task.get('queryIds', []))
        bound = [query for query in queries if not identifiers or query['id'] in identifiers]
        if not bound or (identifiers and identifiers != {query['id'] for query in bound}):
            continue
        scope = {(query['toolName'], query['args']['cost_type'], query['args']['remark']) for query in bound}
        if scope != {tuple(scopes[0])}:
            continue
        for key in TASK_CONDITIONS:
            canonical = 'budgetTargetMonth' if key == 'targetMonth' and task['tool'] == 'budget' else key
            if key in task and canonical in context and task[key] == context[canonical]:
                task.pop(key)


def _keep_source_labels(view):
    groups = view.pop('sourceGroups', [])
    labels = [*view.get('sourceLabels', []), *(group['label'] for group in groups if group.get('label'))]
    if labels:
        view['sourceLabels'] = list(dict.fromkeys(labels))


def compact_user_context(context):
    """Build a model-only view. Use the original conditions for execution and inheritance."""
    context = context or {}
    view = _copy_without_defaults(context)
    for key in ('changes', 'protectedCategories'):
        if key in context and context[key] == []:
            # A resolved scenario can explicitly have no active assumptions or protections.
            view[key] = []
    if context.get('scenarios'):
        view['scenarios'] = [compact_user_context(scenario) for scenario in context['scenarios']]

    _keep_source_labels(view)
    conversation = view.get('conversation', {})
    _keep_source_labels(conversation)
    queries = _logical_queries(conversation.get('queries', []), has_window=bool(conversation.get('analysisWindow')))
    if queries:
        conversation['queries'] = queries

    analyses = conversation.get('analyses', [])
    active = view.get('activeAnalyses', [])
    _remove_duplicate_conditions(analyses, view, queries)
    _remove_duplicate_conditions(active, view, queries)
    history_window = conversation.get('analysisWindow')
    active_window = context.get('activeHistoryWindow', history_window)
    if active and active == [task for task in analyses if task['tool'] != 'sources'] and active_window == history_window:
        view.pop('activeAnalyses')
        view.pop('activeHistoryWindow', None)
    elif active and 'activeHistoryWindow' in context and active_window is None and history_window is not None:
        view['activeHistoryWindow'] = None
    return view
