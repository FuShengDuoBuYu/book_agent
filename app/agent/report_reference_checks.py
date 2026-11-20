"""Validate the role, ledger and calendar of quantities used in prose."""

from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

SLOT = re.compile(r'\{\{([^{}]+)\}\}')


def _month(value, context):
    binding = value.get('binding', {})
    if binding.get('month'):
        return binding['month']
    if binding.get('targetMonth') and binding.get('tool') == 'forecast':
        return binding['targetMonth']
    months = {v['value'] for v in context.values if v['factId'] == value['factId'] and v['unit'] == 'month'}
    return next(iter(months)) if len(months) == 1 else None


def _as_of(context):
    raw = context.payload.get('asOfDate') or context.payload.get('analysisScope', {}).get('asOfDate')
    try:return date.fromisoformat(raw) if raw else datetime.now(ZoneInfo('Asia/Shanghai')).date()
    except (ValueError, TypeError):return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def reference_issues(value, template, start, context):
    if value['unit'] == 'label':
        return []
    issues = []
    prefix = template[:start]
    boundary = max((prefix.rfind(mark) for mark in '。；，,\n'), default=-1)
    sentence_start = max((prefix.rfind(mark) for mark in '。；\n'), default=-1) + 1
    # Quantity roles may follow the value (e.g. X is your available balance).
    end = template.find('}}', start) + 2
    suffix = re.split(r'[。；，,\n]|\{\{', template[end:], maxsplit=1)[0]
    fragment = prefix[boundary + 1:] + suffix
    scope = value.get('binding', {}).get('scope')
    preceding_scopes = list(re.finditer(r'个人版|家庭版', prefix[sentence_start:]))
    named_scope = preceding_scopes[-1][0] if preceding_scopes else None
    following_scope = re.search(r'个人版|家庭版', suffix)
    for name in [named_scope, following_scope[0] if following_scope else None]:
        if name and scope and name != scope and not re.search(r'不能(?:将|把)|不等于|不能代表', fragment):
            issues.append('数量所属账本不能重新命名')
    role = value['role']
    if value['unit'] == '元' and re.search(r'余额|存款|可动用资金|现金(?!流|缺口)', fragment):
        # A calendar average/net is a flow, not an available cash stock.
        if not re.search(r'不能(?:将|把)|不等于|不能代表', fragment):
            stock = role in {'当前可动用余额', '用户提供的可动用余额', 'startingBalance', '用户给定起始余额'}
            future = role in {'cashAfterPurchase', 'projectedBalance'} and not re.search(r'目前|现在|当前|现有', fragment)
            target = role in {'用户目标', 'targetAmount', '希望达到的存款目标'} and bool(re.search(r'目标|希望|存到', fragment))
            reserve = role in {'储备底线', 'reserveAmount', '需要保留的应急储备'} and bool(re.search(r'储备|应急|保留', fragment))
            if not (stock or future or target or reserve):
                issues.append('月均结余历史净额或目标不能当作当前可动用余额')
    if value['unit'] == '元' and isinstance(value['value'], (int, float)):
        if value['value'] < 0 and re.search(r'缺口(?:为|是)?\s*$|还差\s*$|短缺\s*$|相差\s*$', fragment):
            issues.append('负余量不能写成负缺口；请用提供的绝对值引用或保留余量措辞')
        if ('绝对值' in role and not re.search(r'幅度|绝对值|缺|还差|差额|短缺|亏损|减少|增加|底线少|底线多', fragment)
                and not re.search(r'比[^。；]{1,40}(?:多|少|高|低)(?:出|了|于)?\s*$', prefix[boundary + 1:])):
            issues.append('绝对值幅度不能改写成带符号的原始金额')
    if value['unit'] == '元' and re.search(r'预计|预测|下个月.{0,5}(?:收入|支出)', fragment):
        if value.get('binding', {}).get('tool') not in {'forecast', 'projection', 'scenario', 'savings', 'budget', 'runway'}:
            issues.append('历史统计金额不能直接写成未来预测')
    verified_month = _month(value, context)
    relative = re.search(r'(本月|这个月|上个月|上月|下个月|下月)[^。；，]{0,14}(?:收入|支出|结余|进账|入账|金额|花费)[^。；，]{0,6}', fragment)
    if relative:
        current = _as_of(context)
        delta = -1 if relative[1] in {'上个月', '上月'} else 1 if relative[1] in {'下个月', '下月'} else 0
        serial = current.year * 12 + current.month - 1 + delta
        expected = f'{serial // 12:04}-{serial % 12 + 1:02}'
        if verified_month != expected:
            issues.append('不能把其他月份或跨月汇总改称本月或相对月份金额')
    inline = []
    lookup = {v['id']: v for v in context.values}
    for match in SLOT.finditer(fragment):
        item = lookup.get(match[1])
        if item and item['unit'] in {'month', 'date'}:
            inline.append(str(item['value'])[:7])
    if inline and value['unit'] == '元' and verified_month and inline[-1] != verified_month:
        issues.append('日期引用与金额所属月份不一致')
    return issues


def quantitative_text(raw, context):
    # A recent comparison is an ordinal reference, not a newly calculated count.
    text = raw.replace('最近一次', '最近的')
    if '半年' not in context.payload.get('question', ''):
        return text
    windows = [context.payload.get('analysisScope', {}).get('historyWindow')]
    windows += [f.get('binding', {}).get('historyWindow') for f in context.selected]
    for window in windows:
        if not isinstance(window, dict):continue
        try:
            days = (date.fromisoformat(window['endDate']) - date.fromisoformat(window['startDate'])).days
        except (KeyError, ValueError, TypeError):continue
        if 150 <= days <= 190:
            return re.sub(r'(?:最近|近|过去)半年', '所选历史区间', text)
    # Earlier saved evidence may expose six calendar buckets instead of a date window.
    months = set()
    for fact in context.selected:
        binding = fact.get('binding', {})
        if binding.get('aggregation') != 'recorded_month_total' or binding.get('primary') is False:continue
        try:
            month = date.fromisoformat(binding['month'] + '-01')
            months.add(month.year * 12 + month.month)
        except (KeyError, ValueError, TypeError):continue
    ordered = sorted(months)
    today = _as_of(context)
    current = today.year * 12 + today.month
    if len(ordered) == 6 and ordered == list(range(ordered[0], ordered[0] + 6)) and ordered[-1] in {current, current - 1}:
        return re.sub(r'(?:最近|近|过去)半年', '所选历史区间', text)
    return text
