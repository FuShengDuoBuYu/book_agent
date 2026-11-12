"""Resolve user conditions within a query scope and a single scenario."""

from collections import Counter, defaultdict
from copy import deepcopy
import json

from app.schemas.plan import AgentPlan, ScenarioChange, ToolCall

FIELDS = ('months', 'startingBalance', 'targetAmount', 'monthlyImprovement',
          'purchaseAmount', 'reserveAmount', 'targetMonth')
DECISION_TOOLS = {'projection', 'scenario', 'savings', 'optimization', 'purchase', 'runway'}


def latest_context(messages):
    for message in reversed(messages):
        if message.get('role') == 'assistant':
            return message.get('metadata', {}).get('analysisContext', {})
    return {}


def merge_changes(previous, current):
    # Replace previous conditions for a source, preserving multiple changes
    # stated together (e.g. two independent additional income streams).
    replaced = {(row['metric'], row.get('source') or '') for row in current}
    return [row for row in previous if (row['metric'], row.get('source') or '') not in replaced] + deepcopy(current)


def resolve_group(tasks, previous, scope):
    state = deepcopy(previous)
    state.pop('previousMonths', None)
    delta = {task.monthsDelta for task in tasks if task.monthsDelta is not None}
    if len(delta) > 1:
        raise ValueError('Multiple incompatible deadline changes')
    if delta:
        if not state.get('months'):
            raise ValueError('延长期限需要上一轮明确的月数，请确认期限')
        state['previousMonths'] = state['months']
        state['months'] += delta.pop()
        if not 1 <= state['months'] <= 600:
            raise ValueError('Resulting horizon must be between 1 and 600 months')
    for field in FIELDS:
        values = {getattr(task, field) for task in tasks if getattr(task, field) is not None
                  and not (field == 'months' and task.monthsDelta is not None)}
        if len(values) > 1:
            raise ValueError('同一情景存在互相冲突的条件，请分成独立情景')
        if values:
            state[field] = values.pop()
    new_sets = {json.dumps([row.model_dump() for row in task.changes], sort_keys=True, ensure_ascii=False)
                for task in tasks if task.changes}
    if len(new_sets) > 1:
        raise ValueError('不同分析提供了不一致的变化条件，请分别明确情景')
    changes = json.loads(new_sets.pop()) if new_sets else []
    state['changes'] = merge_changes(state.get('changes', []), changes)
    state['protectedCategories'] = list(dict.fromkeys(state.get('protectedCategories', []) +
                                                     [name for task in tasks for name in task.protectedCategories]))
    state['scope'] = [list(row) for row in scope]
    if all(state.get(key) is not None for key in ('startingBalance', 'targetAmount', 'months')):
        # The tool calculates the additional gap; never persist model arithmetic.
        state.pop('monthlyImprovement', None)
    for task in tasks:
        for field in FIELDS:
            setattr(task, field, state.get(field))
        task.changes = [ScenarioChange.model_validate(row) for row in state['changes']]
        task.protectedCategories = state['protectedCategories']
        task.monthsDelta = None
    return state


def resolve_context(plan: AgentPlan, previous: dict, book_mode='个人版') -> dict:
    if plan.analyses and not plan.toolCalls:
        tool = 'search_family_orders' if book_mode == '家庭版' else 'search_personal_orders'
        plan.toolCalls = [ToolCall(id='ledger', toolName=tool)]
    sources = {call.id or f'query_{index}': (call.toolName, call.args.cost_type, call.args.remark)
               for index, call in enumerate(plan.toolCalls, 1)}
    groups = defaultdict(list)
    for task in plan.analyses:
        if task.tool in DECISION_TOOLS:
            ids = task.queryIds or list(sources)
            if set(ids) - sources.keys():
                raise ValueError('An analysis references an unknown query id')
            scope = tuple(sorted({sources[key] for key in ids}))
            if len(scope) > 1:
                raise ValueError('决策条件需要绑定单一账本与筛选范围，不能混用余额或假设')
            groups[scope].append(task)
    states = []
    for scope, tasks in groups.items():
        inherit = plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext
        inherit = inherit and previous.get('scope') == [list(row) for row in scope]
        if inherit and previous.get('ambiguous'):
            raise ValueError('上轮比较了多个情景，请明确要沿用的情景及条件')
        prior = previous if inherit else {}
        alternatives = any(count > 1 for count in Counter(task.tool for task in tasks).values())
        batches = [[task] for task in tasks] if alternatives else [tasks]
        states.extend(resolve_group(batch, prior, scope) for batch in batches)
    if len(states) == 1:
        state = states[0]
    elif states:
        state = {'ambiguous': True, 'scenarios': states}
    else:
        state = deepcopy(previous) if plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext else {}
    plan._analysis_context = state
    return state
