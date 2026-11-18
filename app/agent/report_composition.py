"""Compose report sections whose evidence is bound before drafting the prose."""

import re
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.report_context import _artifact_previews, _ensure_fit, _fit_repair_evidence, _prompt_budget
from app.schemas.report import ReportDraft


OUTLINE_TOKENS = 768
COMMENT_TOKENS = 768
OUTLINE_PROMPT = '''根据本轮问题、用户条件和账本证据拟定简短提纲。
answerGoal 必须逐字选用 answerGoals 中用户已经要求的目标，不能将自己的判断变成新目标。title 选择中性标题，factIds 只绑定真实的事实编号，a 开头的表格编号不能作事实引用。
只写有必要且有证据的段落，一段也可以。金额、日期和表格由程序展示。'''
COMMENT_PROMPT = '''你是记账助手，正在直接回复用户。本段要回答的问题是 section.answerGoal。analysis 就是给用户看的最终答复，请用两至三句话先回答问题，再说明对用户的实际意义，需要时给出具体核查或调整建议。
computedFacts 是程序已经完成的核算结果，会原样显示在你的解释前；表格和备注明细也由程序交付。你直接衔接这些结果，不重做表格，不介绍任务、步骤、字段或生成过程。条件推演已经应用用户假设，不能再扣减或重新计算。
账本说明已记录的变化，不证明未来收入稳定、医疗以后不再发生、生活负担过高或记录完整。未知余额时先确认余额和近期支出，不据月结余判断买得起。正文不用内部编号或代码字段名。
corrections 只说明上一版的错误，须据此修正，不要把纠错意见抄成新的财务事实或用户要求。
如有 values，可按 {{v1}} 的格式引用实际提供的变量，变量已带单位。'''
SECTION_DIAGNOSTIC = re.compile(r'^第\s*(\d+)\s*段')


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


def _section_diagnostics(issues, previous, section_number, title, identifiers):
    result = []
    for issue in issues:
        location = SECTION_DIAGNOSTIC.match(issue)
        if not location:
            result.append(issue)
            continue
        number = int(location[1])
        if 1 <= number <= len(previous):
            old = previous[number - 1]
            if old.get('title') == title or set(old.get('factIds', [])) & identifiers:
                result.append(issue)
        elif number == section_number:
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
    briefs = [(section.title, section.answerGoal, tuple(section.factIds)) for section in outline.sections]
    used = set()
    for _, _, identifiers in briefs:
        if set(identifiers) - available.keys():
            raise ValueError('报告提纲引用了未提供的证据编号')
        used.update(identifiers)
    if any(goal not in goals for _, goal, _ in briefs):
        raise ValueError('报告提纲改变了用户已经确定的回答目标')

    missing = [row['id'] for row in facts if row.get('required') and row['id'] not in used]
    remaining = [missing[index:index + 8] for index in range(0, len(missing), 8)]
    if len(briefs) + len(remaining) > 6:
        raise ValueError('必需证据超出报告段落容量')

    prior = payload.get('previousReport')
    previous = prior.get('sections', []) if isinstance(prior, dict) else []
    limit, estimate = _prompt_budget(COMMENT_PROMPT, ReportComment, num_ctx, COMMENT_TOKENS)
    comment_inputs = []
    for number, (title, goal, identifiers) in enumerate(briefs, 1):
        selected = set(identifiers)
        comment = {**common, 'section': {'title': title, 'answerGoal': goal},
                   'computedFacts': [_subset(available[key], ('text', 'binding')) for key in identifiers]}
        if 'values' in payload:
            comment['values'] = {key: value.split(' ', 1)[1] for key, value in payload['values'].items()
                                 if value.split(' ', 1)[0] in selected}
        diagnostics = _section_diagnostics(corrections, previous, number, title, selected)
        if diagnostics:
            comment['corrections'] = diagnostics
        _ensure_fit(comment, estimate, limit)
        comment_inputs.append(comment)

    sections = []
    for (title, _, identifiers), comment in zip(briefs, comment_inputs):
        drafted = ReportComment.model_validate(await invoke(ReportComment, COMMENT_PROMPT, deepcopy(comment)))
        sections.append({'title': title, 'factIds': list(identifiers), 'analysis': drafted.analysis})
    sections.extend({'title': '补充账本依据', 'factIds': identifiers, 'analysis': ''} for identifiers in remaining)
    return ReportDraft(sections=sections)
