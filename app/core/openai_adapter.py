"""Strict Responses schemas without changing the generic compatible gateway."""

from copy import deepcopy

from langchain_openai import ChatOpenAI


def strict_schema(schema):
    schema = deepcopy(schema)

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            node.pop('default', None)
            if 'const' in node:
                node['enum'] = [node.pop('const')]
            if node.get('type') == 'object' or 'properties' in node:
                if isinstance(node.get('additionalProperties'), dict):
                    raise ValueError('OpenAI strict output needs explicitly named object fields')
                node['additionalProperties'] = False
                node['required'] = list(node.get('properties', {}))
            for child in node.values():
                visit(child)

    visit(schema)
    return schema


def response_format(name, schema):
    return {'type': 'json_schema', 'json_schema': {
        'name': name, 'strict': True, 'schema': strict_schema(schema)}}


def response_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(block.get('text', '') for block in content
                       if isinstance(block, dict) and block.get('type') in {'text', 'output_text'}
                       and isinstance(block.get('text'), str))
    raise TypeError('OpenAI response content is not text')


class LedgerOpenAI(ChatOpenAI):
    """Normalize Responses text blocks for the existing JSON and SSE consumers."""

    async def _agenerate(self, *args, **kwargs):
        result = await super()._agenerate(*args, **kwargs)
        for generation in result.generations:
            generation.message.content = response_text(generation.message.content)
        return result

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        for generation in result.generations:
            generation.message.content = response_text(generation.message.content)
        return result

    async def _astream(self, *args, **kwargs):
        async for chunk in super()._astream(*args, **kwargs):
            chunk.message.content = response_text(chunk.message.content)
            yield chunk

    def _stream(self, *args, **kwargs):
        for chunk in super()._stream(*args, **kwargs):
            chunk.message.content = response_text(chunk.message.content)
            yield chunk
