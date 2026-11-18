"""Typed, fact-scoped quantities; ledger descriptions are never a numeric source."""

import math
import re
from decimal import Decimal, InvalidOperation


REFERENCE = re.compile(r'\{\{(v\d+)\}\}')
QUANTITY = re.compile(r'\d|一半|减半|翻倍|[零〇一二两三四五六七八九十百千万亿半]+\s*(?:元|块|%|％|个月|月的|成|分之)')
CALENDAR = re.compile(r'(?<!\d)(\d{4})\s*[-年/.]\s*(\d{1,2})(?:\s*月(?:份)?)?(?:[-/.](\d{1,2})日?)?(?!\d)')
FULL_DATE = re.compile(r'(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})日?(?!\d)')
YEAR_PREFIX = r'\d{4}\s*[-年/.]\s*'
MONTH_TOKEN = re.compile(rf'(?:(\d{{4}})\s*[-年/.]\s*)?(\d{{1,2}})\s*(?:月(?:份)?)?')
RANGE_MARKS = {'至', '到', '-', '～', '~', '—', '–'}
MONTH_JOIN = r'(?:[、,，～~—–-]|至|到|和|及|与)'
MONTH_SERIES = re.compile(
    rf'(?<![\d./-])(?P<months>(?:{MONTH_TOKEN.pattern}\s*{MONTH_JOIN}\s*)+'
    rf'(?:{YEAR_PREFIX}\d{{1,2}}(?:\s*月(?:份)?)?|\d{{1,2}}\s*月(?:份)?))(?!\d)'
    r'(?:\s*这\s*(?P<count>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*个\s*月)?'
)
DATE_UNIT = re.compile(r'\s*(?!笔数|笔均|天数|次数|次月)(?:元|块|%|％|个月|笔|天|次)')
ORDINAL = re.compile(r'(?m)^[ \t]*(?:#{1,6}[ \t]+)?(?:[（(]\d{1,2}[）)]|\d{1,2}[.)）、．])[ \t]*(?!元|块|个月|笔|天|次)(?=[A-Za-z\u3400-\u9fff*])')
NUMBER_UNIT = re.compile(r'([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(元|块|%|％|个月|笔|天|次|个|项)')


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


def _month_count_matches(raw, count):
    if raw is None or raw == str(count):
        return True
    digits = '零一二三四五六七八九'
    if count < 10:
        return raw.replace('两', '二') == digits[count]
    if count < 100:
        tens, units = divmod(count, 10)
        return raw == (digits[tens] if tens > 1 else '') + '十' + (digits[units] if units else '')
    return False


def _series_months(tokens, text, year, known):
    """Expand adjacent month ranges and carry the year through a month list."""
    covered = set()
    previous = None
    for token in tokens:
        explicit, raw_month = token.groups()
        month = int(raw_month)
        if not 1 <= month <= 12:
            return None
        if explicit:
            year = int(explicit)
        end = year * 12 + month - 1
        start = end
        if previous:
            prev, prev_index = previous
            if text[prev.end():token.start()].strip() in RANGE_MARKS:
                if end < prev_index and not explicit:
                    year += 1
                    end += 12
                if end < prev_index or end - prev_index + 1 > len(known):
                    return None
                start = prev_index
        covered.update(range(start, end + 1))
        previous = (token, end)
    return covered if covered <= known else None


def _validate_calendars(text, allowed, issues):
    known = {(int(v[:4]) * 12 + int(v[5:7]) - 1) for v in allowed
             if re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])(?:-\d{2})?', v)}

    def series(match):
        body = match['months']
        tokens = list(MONTH_TOKEN.finditer(body))
        first_year = tokens[0][1]
        if first_year:
            years = {int(first_year)}
        else:
            # An immediately preceding dated clause anchors shorthand. Otherwise the
            # cited calendar must determine a single year for the entire expression.
            clause = re.split(r'[。！？!?；;\n]', text[:match.start()])[-1]
            anchors = list(CALENDAR.finditer(clause))
            years = {int(anchors[-1][1])} if anchors else {index // 12 for index in known}
        supported = [months for year in years if (months := _series_months(tokens, body, year, known))]
        if len(supported) != 1 or not _month_count_matches(match['count'], len(supported[0])) or DATE_UNIT.match(match.string, match.end()):
            issues.append('月份或月份数量缺少本段引用的连续日期依据：' + match[0])
        return ' '

    def single(match):
        year, month, day = match.groups()
        value = f'{int(year):04}-{int(month):02}' + (f'-{int(day):02}' if day else '')
        valid = value in allowed if day else int(year) * 12 + int(month) - 1 in known and 1 <= int(month) <= 12
        if not valid or DATE_UNIT.match(match.string, match.end()):
            issues.append('日期缺少本段引用的依据或所附单位不符：' + match[0])
        return ' '

    def exact_day(match):
        single(match)
        # Preserve offsets so following shorthand can inherit this date's year.
        return ' ' * len(match[0])

    dated = FULL_DATE.sub(exact_day, text)
    return CALENDAR.sub(single, MONTH_SERIES.sub(series, dated))


def numerical_issues(text, fact_ids, facts):
    catalog = value_catalog(facts)
    by_id = {row['id']: row for row in catalog}
    allowed = [row for row in catalog if row['factId'] in fact_ids]
    issues = []
    for match in REFERENCE.finditer(text):
        row = by_id.get(match[1])
        if not row or row['factId'] not in fact_ids:
            issues.append('数量引用必须来自本段 factIds 所引用的计算事实')
        if re.match(r'\s*(元|块|%|％|个月|笔|天|次|个|项)', text[match.end():]):
            issues.append('数量引用已包含单位，不能另外添加或更换单位')
    remainder = REFERENCE.sub('', text)
    calendars = {str(row['value']) for row in allowed if row['unit'] in {'month', 'date'}}
    calendars.update(value for fact in facts if fact['id'] in fact_ids for value in fact.get('calendars', []))
    remainder = _validate_calendars(remainder, calendars, issues)
    def quantity_range(match):
        first, last, unit = match.groups()
        canonical = {'块': '元', '％': '%'}.get(unit, unit)
        candidates = {Decimal(str(row['value'])) for row in allowed
                      if row['unit'] == canonical and isinstance(row['value'], (int, float))}
        return '' if Decimal(first.replace(',', '')) in candidates and Decimal(last.replace(',', '')) in candidates else match[0]
    remainder = re.sub(r'([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:～|~|至|到|—|–)\s*([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(元|块|%|％|个月|笔|天|次|个|项)', quantity_range, remainder)
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
    remainder = ORDINAL.sub('', remainder)
    if QUANTITY.search(remainder):
        issues.append('未核实的数量或日期：' + remainder[:240] + '；请引用本段计算事实的相同指标与单位，或使用 values 变量')
    return list(dict.fromkeys(issues))
