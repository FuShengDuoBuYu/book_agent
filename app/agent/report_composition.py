"""Compose report sections whose evidence is bound before drafting the prose."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.analysis_context import DECISION_TOOLS
from app.agent.report_context import _artifact_previews, _ensure_fit, _fit_repair_evidence, _prompt_budget
from app.schemas.report import ReportDraft


OUTLINE_TOKENS = 768
COMMENT_TOKENS = 768
OUTLINE_PROMPT = '''根据本轮问题、用户条件和账本证据拟定简短提纲。
answerGoal 必须逐字选用 answerGoals 中用户已经要求的目标，不能将自己的判断变成新目标。title 选择中性标题，factIds 只绑定真实的事实编号，a 开头的表格编号不能作事实引用。
只写有必要且有证据的段落，一段也可以。金额、日期和表格由程序展示。'''
COMMENT_PROMPT = '''你是记账助手，正在直接回复用户。本段要回答的问题是 section.answerGoal。analysis 就是给用户看的最终答复，请用两至三句话先回答问题，再说明对用户的实际意义，需要时给出具体核查或调整建议。
computedFacts 是程序已经完成的核算结果，会原样显示在你的解释前；表格和备注明细也由程序交付。你直接衔接这些结果，不重做表格，不介绍任务、步骤、字段或生成过程。条件推演已经应用用户假设，不能再扣减或重新计算。
若有 section.answerGoals，合并回应所列目标。计算结果的主结论优先，原始历史总额、样本和分项各自按所属口径解读。availableEvidence 说明本次其他证据的范围和类型，具体数值仍以本段 computedFacts 为准。
账本说明已记录的变化，不证明未来收入稳定、医疗以后不再发生、生活负担过高或记录完整。未知余额时先确认余额和近期支出，不据月结余判断买得起。正文不用内部编号或代码字段名。
corrections 只说明上一版的错误，须据此修正，不要把纠错意见抄成新的财务事实或用户要求。
如有 values，可按 {{v1}} 的格式引用实际提供的变量，变量已带单位。'''
SECTION_DIAGNOSTIC = re.compile(r'^第\s*(\d+)\s*段')
SCOPE_FIELDS = ('scope', 'historyWindow', 'filters', 'queryIds')


class OutlineSection(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    title: Literal['分析结论', '变化来源', '收入构成', '条件与方案', '统计口径', '记录核查', '需要确认的事项']
    answerGoal: str = Field(min_length=1, max_length=240)
    factIds: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1, max_length=8)


class ReportOutline(BaseModel):
    model_config = ConfigDict(extra='forbid')

    sections: list[OutlineSection] = Field(min_length=1, max_length=3)


class ReportComment(BaseModel):
    model_config = ConfigDict(extra='forbid')

    analysis: str = Field(max_length=1000, description='直接回答用户本段问题的最终答复。使用已完成的核算结果，不重新计算，不介绍内部处理过程。')


def outline_schema(payload):
    """Constrain native generation to the actual user goals and available facts."""
    schema = ReportOutline.model_json_schema()
    fields = schema['$defs']['OutlineSection']['properties']
    fields['answerGoal']['enum'] = payload['answerGoals']
    fields['factIds']['items']['enum'] = [row['id'] for row in payload['facts']]
    return schema


def _subset(source, fields):
    return {key: source[key] for key in fields if key in source}


def _serialized(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _evidence_inventory(facts):
    records = {}
    for fact in facts:
        binding = fact.get('binding', {})
        if not binding.get('tool'):
            continue
        item = _subset(binding, ('tool', 'scope', 'historyWindow', 'metric', 'aggregation'))
        entry = records.setdefault(_serialized(item), item)
        if binding.get('source') or binding.get('sourceKey'):
            entry['sourceBreakdown'] = True
        if binding.get('member') or binding.get('memberKey'):
            entry['memberBreakdown'] = True
    return list(records.values())


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
        if (not candidates and len(primary_tools) == 1 and binding.get('historyWindow') and
                binding['tool'] not in DECISION_TOOLS):
            candidates = [(key, head) for key, head in headlines if _same_scope(head, binding)]
        if len({head['resultGroup'] for _, head in candidates}) == 1:
            dependencies[fact['id']] = tuple(key for key, _ in candidates)
    return dependencies


@dataclass(frozen=True)
class _Brief:
    titles: tuple[str, ...]
    goals: tuple[str, ...]
    fact_ids: tuple[str, ...]
    selected_ids: frozenset[str]
    positions: tuple[int, ...]
    group: str | None


def _bind_briefs(outline, facts):
    dependencies = _headline_dependencies(list(facts.values()))
    briefs = []
    for position, section in enumerate(outline.sections, 1):
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
        brief = _Brief((section.title,), (section.answerGoal,), bound, frozenset(selected), (position,), group)
        for index, previous in enumerate(briefs):
            same_evidence = brief.selected_ids == previous.selected_ids
            if not (same_evidence or (brief.group and brief.group == previous.group)):
                continue
            combined = tuple(dict.fromkeys((*previous.fact_ids, *brief.fact_ids)))
            if len(combined) <= 8:
                briefs[index] = _Brief(
                    (*previous.titles, *brief.titles), tuple(dict.fromkeys((*previous.goals, *brief.goals))),
                    combined, previous.selected_ids | brief.selected_ids, (*previous.positions, *brief.positions),
                    brief.group,
                )
                break
        else:
            briefs.append(brief)
    return briefs


def _section_diagnostics(issues, previous, brief):
    result = []
    for issue in issues:
        location = SECTION_DIAGNOSTIC.match(issue)
        if not location:
            result.append(issue)
            continue
        number = int(location[1])
        if 1 <= number <= len(previous):
            old = previous[number - 1]
            if old.get('title') in brief.titles or set(old.get('factIds', [])) & brief.selected_ids:
                result.append(issue)
        elif number in brief.positions:
            result.append(issue)
    return result


async def compose_report(payload, invoke, *, num_ctx=8192) -> ReportDraft:
    """Select section evidence once, then ask for prose confined to each selection."""
    common = _subset(payload, ('question', 'analysisScope', 'userConditions'))
    facts = [_subset(row, ('id', 'text', 'required', 'binding')) for row in payload.get('facts', [])]
    if not facts or len({row['id'] for row in facts}) != len(facts):
        raise ValueError('报告提纲需要唯一、可引用的证据编号')

    corrections = payload.get('corrections', [])
    goals = list(dict.fromkeys((payload.get('understanding') or {}).get('requestedAnswers') or [payload['question']]))
    outline_input = {**common, **_subset(payload, ('understanding', 'requestedAnswers', 'corrections')),
                     'answerGoals': goals, 'facts': facts}
    if 'artifacts' in payload:
        outline_input['artifacts'] = _artifact_previews(payload['artifacts'])
    limit, estimate = _prompt_budget(OUTLINE_PROMPT, ReportOutline, num_ctx, OUTLINE_TOKENS)
    outline_input = _fit_repair_evidence(outline_input, estimate, limit)
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
    missing = [row['id'] for row in facts if row.get('required') and row['id'] not in used]
    remaining = [missing[index:index + 8] for index in range(0, len(missing), 8)]
    if len(briefs) + len(remaining) > 6:
        raise ValueError('必需证据超出报告段落容量')

    prior = payload.get('previousReport')
    previous = prior.get('sections', []) if isinstance(prior, dict) else []
    limit, estimate = _prompt_budget(COMMENT_PROMPT, ReportComment, num_ctx, COMMENT_TOKENS)
    inventory = _evidence_inventory(facts)
    comment_inputs = []
    for brief in briefs:
        selected = set(brief.fact_ids)
        section = {'title': brief.titles[0], 'answerGoal': brief.goals[0]}
        if len(brief.goals) > 1:
            section['answerGoals'] = list(brief.goals)
        comment = {**common, 'section': section,
                   'computedFacts': [_subset(bound_facts[key], ('text', 'binding')) for key in brief.fact_ids],
                   'availableEvidence': inventory}
        if 'values' in payload:
            comment['values'] = {key: value.split(' ', 1)[1] for key, value in payload['values'].items()
                                 if value.split(' ', 1)[0] in selected}
        diagnostics = _section_diagnostics(corrections, previous, brief)
        if diagnostics:
            comment['corrections'] = diagnostics
        _ensure_fit(comment, estimate, limit)
        comment_inputs.append(comment)

    sections = []
    for brief, comment in zip(briefs, comment_inputs):
        drafted = ReportComment.model_validate(await invoke(ReportComment, COMMENT_PROMPT, deepcopy(comment)))
        sections.append({'title': brief.titles[0], 'factIds': list(brief.fact_ids), 'analysis': drafted.analysis})
    sections.extend({'title': '补充账本依据', 'factIds': identifiers, 'analysis': ''} for identifiers in remaining)
    return ReportDraft(sections=sections)
