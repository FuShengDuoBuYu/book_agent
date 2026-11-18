"""Typed, fact-scoped quantities; ledger descriptions are never a numeric source."""

import math
import re
from decimal import Decimal, InvalidOperation


REFERENCE = re.compile(r'\{\{(v\d+)\}\}')
QUANTITY = re.compile(r'\d|一半|减半|翻倍|[零〇一二两三四五六七八九十百千万亿半]+\s*(?:元|块|%|％|个月|月的|成|分之)')
CALENDAR = re.compile(r'(?<!\d)(\d{4})\s*[-年/.]\s*(\d{1,2})(?:\s*月)?(?:[-/.](\d{1,2})日?)?(?!\d)')
NUMBER_UNIT = re.compile(r'([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(元|块|%|％|个月|笔|天|次)')


def literal(value):
    text = str(value).replace('\n', ' ').replace('\r', ' ')
    fence = '`' * (max((len(match[0]) for match in re.finditer(r'`+', text)), default=0) + 1)
    return f'{fence} {text} {fence}'


def quantity(label, value, unit='元'):
    return {'label': label, 'value': value, 'unit': unit}


def literal_links(text):
    # This analyst has no web retrieval tool. A ledger URL is a quoted record,
    # not an external source citation or an instruction to open a payment page.
    return re.sub(r'!?\[[^\]\n]+\](?:\([^\n]*?\)|\[[^\]\n]*\])?|<https?://[^>\n]+>|https?://[^\s`<>]+|www\.[^\s`<>]+',
                  lambda match: literal(match[0]), text)


def sanitize_fact(text, result):
    """Quote only ledger-origin spans capable of changing Markdown/linkification."""
    fields = {'source', 'member', 'memberName', 'name', 'category', 'remark', 'remarks', 'afterRemarks',
              'incomeSources', 'protectedCategories', 'excludedCategories', 'reason', 'label'}
    strings = set()
    def visit(value, field=''):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, key)
        elif isinstance(value, list):
            for item in value:
                visit(item, field)
        elif isinstance(value, str) and field in fields and value and re.search(r'[\[\]`*_~<>\\\n\r]|https?://|www\.', value):
            strings.add(value)
    visit(result)
    if strings:
        pattern = re.compile('|'.join(re.escape(value) for value in sorted(strings, key=len, reverse=True)))
        text = pattern.sub(lambda match: literal(match[0]), text)
    return text


def value_catalog(facts):
    values = []
    for fact in facts:
        for item in fact.get('values', []):
            value, unit = item.get('value'), item.get('unit')
            if unit in {'month', 'date'}:
                pattern = r'\d{4}-\d{2}' if unit == 'month' else r'\d{4}-\d{2}-\d{2}'
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    continue
                display = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                display = f'{value:,.2f}'.rstrip('0').rstrip('.') + (' ' + unit if unit else '')
            else:
                continue
            values.append({'id': f'v{len(values) + 1}', 'factId': fact['id'],
                           'label': item['label'], 'value': value, 'unit': unit, 'display': display})
    return values


def expand_values(text, facts):
    values = {row['id']: row['display'] for row in value_catalog(facts)}
    return REFERENCE.sub(lambda match: values.get(match[1], match[0]), text)


def numerical_issues(text, fact_ids, facts):
    catalog = value_catalog(facts)
    by_id = {row['id']: row for row in catalog}
    allowed = [row for row in catalog if row['factId'] in fact_ids]
    issues = []
    for match in REFERENCE.finditer(text):
        row = by_id.get(match[1])
        if not row or row['factId'] not in fact_ids:
            issues.append('数量引用必须来自本段 factIds 所引用的计算事实')
        if re.match(r'\s*(元|块|%|％|个月|笔|天|次)', text[match.end():]):
            issues.append('数量引用已包含单位，不能另外添加或更换单位')
    remainder = REFERENCE.sub('', text)
    # A report's known calendar month can also appear in a section heading.
    calendars = {str(row['value']) for row in catalog if row['unit'] in {'month', 'date'}}
    calendars.update(value for fact in facts for value in fact.get('calendars', []))
    def calendar(match):
        year, month, day = match.groups()
        value = f'{int(year):04}-{int(month):02}' + (f'-{int(day):02}' if day else '')
        return '' if value in calendars else match[0]
    def calendar_range(match):
        year, start, end = map(int, match.groups())
        first = f'{year:04}-{start:02}'
        last = f'{year + (end < start):04}-{end:02}'
        return '' if first in calendars and last in calendars else match[0]
    remainder = re.sub(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(?:至|到|～|~|—|-)\s*(\d{1,2})\s*月', calendar_range, remainder)
    remainder = CALENDAR.sub(calendar, remainder)
    def quantity_range(match):
        first, last, unit = match.groups()
        canonical = {'块': '元', '％': '%'}.get(unit, unit)
        candidates = {Decimal(str(row['value'])) for row in allowed
                      if row['unit'] == canonical and isinstance(row['value'], (int, float))}
        return '' if Decimal(first.replace(',', '')) in candidates and Decimal(last.replace(',', '')) in candidates else match[0]
    remainder = re.sub(r'([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:～|~|至|到|—|–)\s*([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(元|块|%|％|个月|笔|天|次)', quantity_range, remainder)
    def quantity(match):
        raw, unit = match.groups()
        canonical = {'块': '元', '％': '%'}.get(unit, unit)
        try:
            value = Decimal(raw.replace(',', ''))
        except InvalidOperation:
            return match[0]
        if any(row['unit'] == canonical and isinstance(row['value'], (int, float))
               and Decimal(str(row['value'])) == value for row in allowed):
            return ''
        return match[0]
    remainder = NUMBER_UNIT.sub(quantity, remainder)
    remainder = re.sub(r'(?m)^\s*\d{1,2}[.)、]\s+', '', remainder)
    if QUANTITY.search(remainder):
        issues.append('未核实的数量或日期：' + remainder[:240] + '；请引用本段计算事实的相同指标与单位，或使用 values 变量')
    return list(dict.fromkeys(issues))
