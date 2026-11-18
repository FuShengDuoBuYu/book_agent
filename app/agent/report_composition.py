"""Compose report sections whose evidence is bound before drafting the prose."""

import re
from copy import deepcopy
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.agent.report_context import _artifact_previews, _ensure_fit, _fit_repair_evidence, _prompt_budget
from app.schemas.report import ReportDraft


OUTLINE_TOKENS = 768
COMMENT_TOKENS = 768
OUTLINE_PROMPT = '''根据本轮问题、用户条件和账本证据拟定简短提纲。
优先覆盖用户要求的交付和判断。每段用中性标题与 answerGoal 说明该段要回答什么，再绑定直接支持它的 factIds。
只写有必要且有证据的段落，一段也可以。金额、日期和表格由程序展示。'''
COMMENT_PROMPT = '''用两至三句回答 section.answerGoal。先给判断，再说明它与本段证据的关系，或指出具体需要核查的信息。
程序会展示已引用的账本事实和表格。数量沿用证据中的原指标、单位和时间，计算交给程序。
如有 values，可按 {{v1}} 的格式引用实际提供的变量，变量已带单位。'''
SECTION_DIAGNOSTIC = re.compile(r'^第\s*(\d+)\s*段')


class OutlineSection(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)
    answerGoal: str = Field(min_length=1, max_length=240)
    factIds: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1, max_length=8)


class ReportOutline(BaseModel):
    model_config = ConfigDict(extra='forbid')

    sections: list[OutlineSection] = Field(min_length=1, max_length=3)


class ReportComment(BaseModel):
    model_config = ConfigDict(extra='forbid')

    analysis: str = Field(max_length=1000)


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
    outline_input = {**common, **_subset(payload, ('understanding', 'requestedAnswers', 'corrections')),
                     'facts': facts}
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
        comment = {**common, 'section': {'title': title, 'answerGoal': goal, 'factIds': list(identifiers)},
                   'facts': [available[key] for key in identifiers]}
        if 'values' in payload:
            comment['values'] = {key: value for key, value in payload['values'].items()
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
