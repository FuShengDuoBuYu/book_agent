"""Parse one monetary literal without guessing a sentence's financial role.

In ordinary financial speech the final lower place may be omitted: 一百二 is
120, 一千二 is 1200 and 一万二 is 12000. An intervening explicit 零 instead
keeps the final digit in the ones place. Explicit lower units always win.
"""

from decimal import Decimal, InvalidOperation
import math
import re


_DIGITS = {char: value for value, char in enumerate('一二三四五六七八九', 1)} | {'两': 2}
_SMALL_UNITS = {'十': 10, '百': 100, '千': 1000}
_OMITTED_UNIT = {'百': '十', '千': '百', '万': '千', '亿': '千万'}
_ARABIC = re.compile(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?万?')
_CHINESE = re.compile(r'[零一二两三四五六七八九十百千万亿]+')
_TRAILING_OMISSION = re.compile(r'([百千万亿])([一二两三四五六七八九])$')


def parse_amount_literal(raw: str) -> int | float | None:
    """Return a nonnegative finite amount, or None for an uncertain literal.

    Approximation, ranges, multiple numbers, mixed Chinese/Arabic notation and
    malformed unit sequences are deliberately left unknown. This function
    does not extract amounts from prose or decide which financial field to use.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > 128:
        return None
    text = re.sub(r'(?:块钱|元|块)$', '', text)
    if _ARABIC.fullmatch(text):
        try:
            value = Decimal(text.removesuffix('万').replace(',', ''))
            if text.endswith('万'):
                value *= 10000
            number = float(value)
        except (InvalidOperation, OverflowError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return int(value) if value == value.to_integral_value() else number
    if not _CHINESE.fullmatch(text):
        return None
    if text == '零':
        return 0
    # Infer only a single final omitted place, never an interior coefficient.
    # Explicit zero and explicit final units cannot match this pattern.
    text = _TRAILING_OMISSION.sub(lambda m: m[0] + _OMITTED_UNIT[m[1]], text)
    if text.count('亿') > 1:
        return None
    if '亿' in text:
        head, tail = text.split('亿')
        upper = _below_yi(head)
        lower = _below_yi(tail, allow_leading_zero=True) if tail else 0
        if upper is None or upper <= 0 or lower is None or lower >= 100_000_000:
            return None
        return upper * 100_000_000 + lower
    return _below_yi(text)


def _below_yi(text: str, *, allow_leading_zero: bool = False) -> int | None:
    if allow_leading_zero and text.startswith('零'):
        text = text[1:]
    if not text or text.count('万') > 1:
        return None
    if '万' not in text:
        return _small_section(text)
    head, tail = text.split('万')
    upper = _small_section(head)
    # A bare digit after 万 is only safe when the whole literal's final
    # omission was expanded above, or the user explicitly supplied 零.
    if tail in _DIGITS:
        return None
    lower = _small_section(tail, allow_leading_zero=True) if tail else 0
    if upper is None or upper <= 0 or lower is None:
        return None
    return upper * 10000 + lower


def _small_section(text: str, *, allow_leading_zero: bool = False) -> int | None:
    if allow_leading_zero and text.startswith('零'):
        text = text[1:]
    if not text:
        return None
    total = 0
    previous_unit = 10000
    pending = None
    zero_separator = False
    pending_after_zero = False
    for char in text:
        if char in _DIGITS:
            if pending is not None:
                return None
            pending = _DIGITS[char]
            pending_after_zero = zero_separator
            zero_separator = False
        elif char == '零':
            if pending is not None or total == 0 or zero_separator:
                return None
            zero_separator = True
        elif char in _SMALL_UNITS:
            unit = _SMALL_UNITS[char]
            if unit >= previous_unit:
                return None
            if pending is None:
                # Only a leading 十 conventionally omits its coefficient 一.
                if unit != 10 or total or zero_separator:
                    return None
                pending = 1
            total += pending * unit
            previous_unit = unit
            pending = None
            zero_separator = False
        else:
            return None
    if zero_separator:
        return None
    if pending is not None:
        # An unexpanded omission inside a larger coefficient is ambiguous.
        if 100 <= previous_unit < 10000 and not pending_after_zero:
            return None
        total += pending
    return total
