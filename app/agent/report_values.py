"""Typed values and inert ledger text for evidence-backed reports."""

import math
import re


QUANTITY_LITERAL = re.compile(
    r'\d|百分之[零〇一二两三四五六七八九十百千万亿]+|[零〇一二两三四五六七八九十百千万亿]+分之[零〇一二两三四五六七八九十百千万亿]+'
    r'|[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+)?\s*'
    r'(?:元|块|％|%|个月|万元|笔|次(?!性)|成|倍|天|日|周|年|月|小时|分钟|秒)|翻倍|翻番|加倍|一半|半(?:个)?(?:月|年|天|日|周|小时|分钟|秒)|半数')


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
