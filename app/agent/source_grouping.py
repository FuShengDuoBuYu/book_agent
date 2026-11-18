"""Check proposed business aliases against authorized source labels and remarks."""

import re
import unicodedata
from collections import defaultdict

from app.schemas.plan import SourceGroup


# These terms describe bookkeeping, parties or settlement, rather than a particular job.
_GENERIC = re.compile(
    r'有限责任公司|股份有限公司|有限公司|工作室|编辑部|公司|'
    r'其他|其它|一般|固定|常规|每月|本月|上月|周末|日常|个人|家庭|成员|'
    r'项目|业务|工作|分类|合计|收入|支出|工资|薪资|薪酬|兼职|劳务|实习|报销|'
    r'收款|付款|转账|到账|入账|结算|结款|月结|尾款|预付|发放|'
    r'初稿|修改|交付|费用|服务费|人民币|'
    r'[〇零一二两三四五六七八九十百千万\d.]+(?:年|月|日|号|元|块)'
)
_RUN = re.compile(r'[\u3400-\u9fff]+|[A-Za-z]+')
_LATIN_GENERIC = {'income', 'expense', 'payment', 'invoice', 'project', 'salary', 'work', 'monthly',
                  'company', 'transfer', 'business', 'amount', 'service', 'services'}
_NAMING = re.compile(r'称为|叫作|叫做|简称为?|记作|记为|命名为|标为|改名为')


def _runs(text, member):
    text = unicodedata.normalize('NFKC', str(text))
    if member:
        text = text.replace(member, ' ')
    return _RUN.findall(_GENERIC.sub(' ', text))


def _anchors(row, suggested, question):
    """Return literal evidence fragments. A two-character alias also needs remark support."""
    display_name = unicodedata.normalize('NFKC', row.get('member') or '')
    member = re.sub(r'\(成员\d+\)$', '', display_name).strip()
    notes = row.get('remarks') or []
    remark_runs = {run for note in notes for run in _runs(note, member)}
    label_runs = _runs(suggested, member)
    anchors = set()
    for run in remark_runs | set(_runs(row['source'], member)):
        if run.isascii():
            if len(run) >= 4 and run.lower() not in _LATIN_GENERIC:
                anchors.add(run)
            continue
        for size in range(2, min(len(run), 20) + 1):
            for start in range(len(run) - size + 1):
                phrase = run[start:start + size]
                if size > 2 or (run in remark_runs and (phrase in label_runs or phrase in question)):
                    anchors.add(phrase)
    return anchors


def _named_by_user(question, source, label):
    # Mentioning the old name and alias in unrelated clauses is insufficient.
    for clause in re.split(r'[,，。；;！？!?\n]', question):
        naming = _NAMING.search(clause)
        if naming and source in clause[:naming.start()] and not re.search(r'不|别|勿', clause[:naming.start()]):
            if clause[naming.end():].strip(' “”“「」『』"') == label:
                return True
    return False


def _subgroups(keys, observations, suggested, question):
    by_anchor = defaultdict(set)
    for key in keys:
        for phrase in _anchors(observations[key], suggested, question):
            by_anchor[phrase].add(key)
    candidates = sorted((p for p, members in by_anchor.items() if len(members) >= 2),
                        key=lambda p: (-len(by_anchor[p]), -len(p), p != suggested, p))
    available = set(keys)
    result = []
    for phrase in candidates:
        if len(available) < 2:
            break
        matched = available & by_anchor[phrase]
        if len(matched) < 2:
            continue
        result.append(SourceGroup(label=phrase, sourceKeys=[key for key in keys if key in matched]))
        available -= matched
    return result


def grounded_source_groups(groups, catalogs, question):
    """Retain independently supported subgroups; all other sources keep their observed keys."""
    observed = {row['sourceKey']: row for catalog in catalogs for row in catalog.get('sources', [])}
    output, issues, used = [], [], set()
    for group in groups:
        keys = group.sourceKeys
        if not keys or len(keys) != len(set(keys)) or set(keys) - observed.keys() or used.intersection(keys):
            issues.append('来源归组含未核实或重复的键，已保留原始来源。')
            continue
        used.update(keys)
        if len(keys) == 1:
            if _named_by_user(question, observed[keys[0]]['source'], group.label):
                output.append(group)
            elif group.label != observed[keys[0]]['source']:
                issues.append('单一来源未获用户明确改名，保留账本原名。')
            continue
        buckets = defaultdict(list)
        for key in keys:
            row = observed[key]
            boundary = (row.get('memberKey') or row.get('member'), row['metric'], bool(row.get('excluded')))
            buckets[boundary].append(key)
        supported = [part for bucket in buckets.values() if len(bucket) >= 2
                     for part in _subgroups(bucket, observed, group.label, question)]
        if supported != [group]:
            issues.append('已按成员、收支方向、排除状态和共同业务词复核归组；缺少共同依据的来源保留原名。')
        output.extend(supported)
    if len(output) > 30:
        issues.append('已达来源归组数量上限，剩余来源保留原名。')
    return output[:30], issues
