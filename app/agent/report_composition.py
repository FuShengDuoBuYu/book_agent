"""Select evidence for a report and render the corresponding computed findings."""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.analysis_context import DECISION_TOOLS
from app.agent.report_context import _artifact_previews, _ensure_fit, _fit_evidence, _prompt_budget
from app.schemas.report import ReportDraft


OUTLINE_TOKENS = 768
OUTLINE_PROMPT = '''根据本轮问题、用户条件和账本证据拟定简短提纲。
answerGoal 必须逐字选用 answerGoals 中用户已经要求的目标，不能将自己的判断变成新目标。title 选择中性标题，factIds 只绑定真实的事实编号，a 开头的表格编号不能作事实引用。
只写有必要且有证据的段落，一段也可以。金额、日期和表格由程序展示。'''
SCOPE_FIELDS = ('scope', 'historyWindow', 'filters', 'queryIds')


class OutlineSection(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    title: Literal['分析结论', '变化来源', '收入构成', '条件与方案', '统计口径', '记录核查', '需要确认的事项']
    answerGoal: str = Field(min_length=1, max_length=240)
    factIds: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1, max_length=8)


class ReportOutline(BaseModel):
    model_config = ConfigDict(extra='forbid')

    sections: list[OutlineSection] = Field(min_length=1, max_length=3)


def outline_schema(payload):
    """Constrain native generation to the actual user goals and available facts."""
    schema = ReportOutline.model_json_schema()
    fields = schema['$defs']['OutlineSection']['properties']
    fields['answerGoal']['enum'] = payload['answerGoals']
    fields['factIds']['items']['enum'] = [row['id'] for row in payload['facts']]
    dependencies = _headline_dependencies(payload['facts'])
    reserve = max((len(heads) for heads in dependencies.values()), default=0)
    fields['factIds']['maxItems'] = max(1, 8 - reserve)
    return schema


def _subset(source, fields):
    return {key: source[key] for key in fields if key in source}


def _serialized(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _same_scope(first, second):
    return bool(first.get('scope')) and all(first.get(key) == second.get(key) for key in SCOPE_FIELDS)


def _headline_dependencies(facts):
    primary_tools = {binding.get('tool') for binding in (fact.get('binding', {}) for fact in facts)
                     if binding.get('primary') is True and binding.get('tool') in DECISION_TOOLS}
    headlines = [(fact['id'], fact['binding']) for fact in facts if fact.get('binding', {}).get('headline') is True
                 and fact['binding'].get('primary') is True and fact['binding'].get('tool') in DECISION_TOOLS
                 and fact['binding'].get('resultGroup')]
    dependencies = {}
    for fact in facts:
        binding = fact.get('binding', {})
        group = binding.get('resultGroup')
        if not (group and binding.get('tool') and binding.get('scope')):
            continue
        candidates = [(key, head) for key, head in headlines if head['resultGroup'] == group
                      and head['tool'] == binding['tool'] and _same_scope(head, binding)]
        if (not candidates and len(primary_tools) == 1 and (binding.get('historyWindow') or binding.get('queryIds')) and
                binding['tool'] not in DECISION_TOOLS):
            candidates = [(key, head) for key, head in headlines if _same_scope(head, binding)]
        if len({head['resultGroup'] for _, head in candidates}) == 1:
            dependencies[fact['id']] = tuple(key for key, _ in candidates)
    return dependencies


@dataclass(frozen=True)
class _Brief:
    title: str
    fact_ids: tuple[str, ...]
    selected_ids: frozenset[str]
    group: str | None


def _primary_rank(identifiers, facts):
    rank = 2
    for key in identifiers:
        binding = facts[key].get('binding', {})
        if binding.get('primary') is True and binding.get('tool') in DECISION_TOOLS:
            if binding.get('headline') is True:
                return 0
            rank = 1
    return rank


def _bind_briefs(outline, facts):
    dependencies = _headline_dependencies(list(facts.values()))
    briefs = []
    for section in sorted(outline.sections, key=lambda item: _primary_rank(item.factIds, facts)):
        selected = tuple(dict.fromkeys(section.factIds))
        bound = tuple(dict.fromkeys((*selected, *(head for key in selected for head in dependencies.get(key, ())))))
        if len(bound) > 8:
            raise ValueError('已选证据加上必要主结论后超过每段 8 条')
        groups = []
        for key in selected:
            anchor = dependencies[key][0] if key in dependencies else key
            binding = facts[anchor].get('binding', {})
            groups.append(_serialized(_subset(binding, ('resultGroup', *SCOPE_FIELDS)))
                          if binding.get('resultGroup') and binding.get('scope') else None)
        group = groups[0] if groups and all(item == groups[0] for item in groups) else None
        brief = _Brief(section.title, bound, frozenset(selected), group)
        for index, previous in enumerate(briefs):
            same_evidence = brief.selected_ids == previous.selected_ids
            if not (same_evidence or (brief.group and brief.group == previous.group)):
                continue
            combined = tuple(dict.fromkeys((*previous.fact_ids, *brief.fact_ids)))
            if len(combined) <= 8:
                briefs[index] = _Brief(previous.title, combined, previous.selected_ids | brief.selected_ids, brief.group)
                break
        else:
            briefs.append(brief)
    return briefs


def grounded_analysis(fact_ids, facts, findings, seen=None) -> str:
    """Render only findings tied to the selected facts, once per result group."""
    indexed = facts if isinstance(facts, dict) else {row['id']: row for row in facts}
    seen = seen if seen is not None else set()
    paragraphs = {}
    for key in fact_ids:
        if key not in indexed:
            raise ValueError('报告引用了未提供的证据编号')
        binding = indexed[key].get('binding', {})
        group, scope = binding.get('resultGroup'), binding.get('scope')
        finding = findings.get(group)
        if not group or not finding:
            raise ValueError('解释缺少与当前计算证据绑定的分析发现')
        if not scope or scope != finding.get('scope') or binding.get('tool') != finding.get('tool'):
            raise ValueError('分析发现的范围或计算类型与引用证据不一致')
        if group in seen or group in paragraphs:
            continue
        statements = [text for field in ('observations', 'limitations', 'actions')
                      for text in (finding.get(field) or []) if text]
        if not statements:
            raise ValueError('解释缺少与当前计算证据绑定的分析发现')
        paragraphs[group] = f"【{scope}】{' '.join(statements)}"
    seen.update(paragraphs)
    return '\n\n'.join(paragraphs.values())


async def compose_report(payload, invoke, *, num_ctx=8192) -> ReportDraft:
    """Ask for an outline once and render the selected program findings."""
    common = _subset(payload, ('question', 'analysisScope', 'userConditions'))
    facts = [_subset(row, ('id', 'text', 'required', 'binding')) for row in payload.get('facts', [])]
    if not facts or len({row['id'] for row in facts}) != len(facts):
        raise ValueError('报告提纲需要唯一、可引用的证据编号')

    goals = list(dict.fromkeys((payload.get('understanding') or {}).get('requestedAnswers') or [payload['question']]))
    outline_input = {**common, **_subset(payload, ('understanding', 'requestedAnswers', 'corrections')),
                     'answerGoals': goals, 'facts': facts}
    if 'artifacts' in payload:
        outline_input['artifacts'] = _artifact_previews(payload['artifacts'])
    limit, estimate = _prompt_budget(OUTLINE_PROMPT, ReportOutline, num_ctx, OUTLINE_TOKENS)
    outline_input = _fit_evidence(outline_input, estimate, limit)
    _ensure_fit(outline_input, estimate, limit)

    available = {row['id']: row for row in outline_input['facts']}
    if not available:
        raise ValueError('报告提纲预算内没有可引用的证据')
    outline = ReportOutline.model_validate(await invoke(ReportOutline, OUTLINE_PROMPT, deepcopy(outline_input)))
    for section in outline.sections:
        if set(section.factIds) - available.keys():
            raise ValueError('报告提纲引用了未提供的证据编号')
    if any(section.answerGoal not in goals for section in outline.sections):
        raise ValueError('报告提纲改变了用户已经确定的回答目标')

    bound_facts = {row['id']: row for row in facts}
    briefs = _bind_briefs(outline, bound_facts)
    used = {key for brief in briefs for key in brief.fact_ids}
    missing = {}
    for row in facts:
        if row.get('required') and row['id'] not in used:
            scope = _serialized(_subset(row.get('binding', {}), SCOPE_FIELDS))
            missing.setdefault(scope, []).append(row['id'])
    ordered = [((_primary_rank(brief.selected_ids, bound_facts), _primary_rank(brief.fact_ids, bound_facts)),
                brief.title, brief.fact_ids) for brief in briefs]
    for identifiers in missing.values():
        identifiers.sort(key=lambda key: _primary_rank((key,), bound_facts))
        for index in range(0, len(identifiers), 8):
            block = identifiers[index:index + 8]
            rank = _primary_rank(block, bound_facts)
            ordered.append(((rank, rank), '补充账本依据', block))

    findings = payload.get('analysisFindings', {})
    seen_facts, seen_groups = set(), set()
    sections = []
    for _, title, identifiers in sorted(ordered, key=lambda item: item[0]):
        first_use = [key for key in sorted(identifiers, key=lambda key: _primary_rank((key,), bound_facts))
                     if key not in seen_facts]
        if not first_use:
            continue
        seen_facts.update(first_use)
        sections.append({'title': title, 'factIds': first_use,
                         'analysis': grounded_analysis(first_use, bound_facts, findings, seen_groups)})
    if len(sections) > 6:
        raise ValueError('必需证据超出报告段落容量')
    return ReportDraft(sections=sections)
