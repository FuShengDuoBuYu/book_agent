"""Resolved user assumptions, independent of generated answer prose."""

from copy import deepcopy

from app.schemas.plan import AgentPlan, ScenarioChange

FIELDS = ('months', 'startingBalance', 'targetAmount', 'monthlyImprovement',
          'purchaseAmount', 'reserveAmount', 'targetMonth')
DECISION_TOOLS = {'projection', 'scenario', 'savings', 'optimization', 'purchase', 'runway'}


def latest_context(messages):
    for message in reversed(messages):
        if message.get('role') == 'assistant':
            return message.get('metadata', {}).get('analysisContext', {})
    return {}


def resolve_context(plan: AgentPlan, previous: dict) -> dict:
    scope = sorted({(call.toolName, call.args.cost_type, call.args.remark) for call in plan.toolCalls})
    scope = [list(row) for row in scope]
    same_scope = not scope or not previous.get('scope') or scope == previous['scope']
    inherit = plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext and same_scope
    state = deepcopy(previous) if inherit else {}
    if inherit and state.get('ambiguous'):
        raise ValueError('上轮比较了多个不同情景，请明确沿用哪一个情景及其条件')
    state.pop('previousMonths', None)
    changes = {(row['metric'], row.get('source') or ''): row for row in state.get('changes', [])}
    protected = state.get('protectedCategories', [])
    deltas = {task.monthsDelta for task in plan.analyses if task.monthsDelta is not None}
    if len(deltas) > 1:
        raise ValueError('Multiple incompatible deadline changes')
    if deltas:
        if not state.get('months'):
            raise ValueError('延长期限需要上一轮明确的月数，请确认期限')
        state['previousMonths'] = state['months']
        state['months'] += deltas.pop()
        if not 1 <= state['months'] <= 600:
            raise ValueError('Resulting horizon must be between 1 and 600 months')
    decisions = [task for task in plan.analyses if task.tool in DECISION_TOOLS]
    alternatives = False
    for field in FIELDS:
        values = {getattr(task, field) for task in decisions if getattr(task, field) is not None
                  and not (field == 'months' and task.monthsDelta is not None)}
        if len(values) == 1:
            state[field] = values.pop()
        elif len(values) > 1:
            alternatives = True
    new_changes = {}
    for task in decisions:
        for change in task.changes:
            key = (change.metric, change.source)
            new_changes.setdefault(key, {})[change.model_dump_json()] = change.model_dump()
    for key, values in new_changes.items():
        if len(values) == 1:
            changes[key] = next(iter(values.values()))
        else:
            alternatives = True
    for task in decisions:
        protected = list(dict.fromkeys(protected + task.protectedCategories))
    state['changes'] = list(changes.values())
    state['protectedCategories'] = protected
    state['scope'] = scope or state.get('scope', [])
    for task in plan.analyses:
        if task.tool in DECISION_TOOLS:
            for field in FIELDS:
                if field in state and (getattr(task, field) is None or (field == 'months' and task.monthsDelta is not None)):
                    setattr(task, field, state[field])
            local_changes = {**changes, **{(row.metric, row.source): row.model_dump() for row in task.changes}}
            task.changes = [ScenarioChange.model_validate(row) for row in local_changes.values()]
            task.protectedCategories = protected
            task.monthsDelta = None
    if alternatives:
        state = {'ambiguous': True, 'scope': state['scope'], 'scenarios': [task.model_dump() for task in decisions]}
    plan._analysis_context = state
    return state
