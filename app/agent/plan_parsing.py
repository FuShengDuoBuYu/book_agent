"""Recover a non-executable annotation shape without repairing tool arguments."""

import json
import re


def container_end(text, start):
    stack, quoted, escaped = [], False, False
    pairs = {']': '[', '}': '{'}
    for i in range(start, len(text)):
        char = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            stack.append(char)
        elif char in ']}':
            if not stack or stack.pop() != pairs[char]:
                return None
            if not stack:
                return i
    return None


def repair_descriptive_arrays(text):
    match = re.search(r'(?<!\\)"understanding"\s*:\s*\{', text)
    if not match:
        return text, []
    start = match.end() - 1
    end = container_end(text, start)
    if end is None:
        return text, []
    block, repairs = text[start:end + 1], []
    pattern = re.compile(r'(?<!\\)"(referents|requestedAnswers|evidenceNeeds|unknowns)"\s*:\s*\[')
    for item in reversed(list(pattern.finditer(block))):
        left = item.end() - 1
        right = container_end(block, left)
        if right is None:
            continue
        try:
            mapping = json.loads('{' + block[left + 1:right] + '}')
        except json.JSONDecodeError:
            continue
        if not mapping or not all(isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()):
            continue
        values = [f'{key}：{value}' for key, value in mapping.items()]
        block = block[:left] + json.dumps(values, ensure_ascii=False) + block[right + 1:]
        repairs.append({'type': 'normalized_descriptive_array', 'field': 'understanding.' + item[1]})
    return text[:start] + block + text[end + 1:], repairs
