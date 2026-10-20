"""Bounded canonical JSON, deliberately smaller than JSON (no null or floats)."""
from __future__ import annotations

import re


class ProtocolError(ValueError):
    pass


MAX_INTEGER = 2**53 - 1
MAX_STRING = 65536
MAX_ITEMS = 1024
MAX_DEPTH = 24
# Matched at an offset: slicing the remaining source per number is quadratic in the frame size.
NUMBER = re.compile(r'-?(?:0|[1-9][0-9]{0,15})')


def encode(value, *, max_string: int = MAX_STRING) -> bytes:
    def visit(item, depth):
        if depth > MAX_DEPTH:
            raise ProtocolError("canonical depth limit")
        if type(item) is bool:
            return "true" if item else "false"
        if type(item) is int and abs(item) <= MAX_INTEGER:
            return str(item)
        if type(item) is str:
            try:
                if len(item.encode("utf-8")) > max_string:
                    raise ProtocolError("canonical string limit")
            except UnicodeError:
                raise ProtocolError("invalid unicode") from None
            return '"' + ''.join('\\"' if c == '"' else '\\\\' if c == '\\' else f'\\u{ord(c):04x}' if ord(c) < 32 else c for c in item) + '"'
        if type(item) is list and len(item) <= MAX_ITEMS:
            return "[" + ",".join(visit(v, depth + 1) for v in item) + "]"
        if type(item) is dict and len(item) <= MAX_ITEMS and all(type(k) is str for k in item):
            return "{" + ",".join(visit(k, depth + 1) + ":" + visit(item[k], depth + 1) for k in sorted(item)) + "}"
        raise ProtocolError("unsupported canonical value")
    return visit(value, 0).encode("utf-8")


def decode(data: bytes, *, limit: int = 65536, max_string: int = MAX_STRING):
    if type(data) is not bytes or len(data) > limit:
        raise ProtocolError("canonical size limit")
    try:
        source = data.decode("utf-8")
    except UnicodeError:
        raise ProtocolError("invalid utf-8") from None
    position = 0

    def parse(depth=0):
        nonlocal position
        if depth > MAX_DEPTH or position >= len(source):
            raise ProtocolError("invalid canonical value")
        char = source[position]
        if char == '"':
            position += 1
            result = []
            while position < len(source):
                char = source[position]
                position += 1
                if char == '"':
                    value = ''.join(result)
                    if len(value.encode("utf-8")) > max_string:
                        raise ProtocolError("canonical string limit")
                    return value
                if char == '\\':
                    if position >= len(source):
                        break
                    escape = source[position]
                    position += 1
                    if escape in {'"', '\\', '/'}:
                        result.append(escape)
                    elif escape in "bfnrt":
                        result.append(dict(b='\b', f='\f', n='\n', r='\r', t='\t')[escape])
                    elif escape == 'u' and re.fullmatch('[0-9a-fA-F]{4}', source[position:position + 4]):
                        code = int(source[position:position + 4], 16)
                        position += 4
                        if 0xd800 <= code <= 0xdfff:
                            raise ProtocolError("invalid unicode")
                        result.append(chr(code))
                    else:
                        raise ProtocolError("invalid escape")
                elif ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff:
                    raise ProtocolError("invalid string")
                else:
                    result.append(char)
            raise ProtocolError("unterminated string")
        if char in "[{":
            position += 1
            result = [] if char == '[' else {}
            end = ']' if char == '[' else '}'
            if position < len(source) and source[position] == end:
                position += 1
                return result
            while True:
                if len(result) >= MAX_ITEMS:
                    raise ProtocolError("canonical item limit")
                if char == '{':
                    key = parse(depth + 1)
                    if type(key) is not str or key in result or source[position:position + 1] != ':':
                        raise ProtocolError("invalid or duplicate object key")
                    position += 1
                    result[key] = parse(depth + 1)
                else:
                    result.append(parse(depth + 1))
                delimiter = source[position:position + 1]
                position += 1
                if delimiter == end:
                    return result
                if delimiter != ',':
                    raise ProtocolError("invalid delimiter")
        for word, value in (("true", True), ("false", False)):
            if source.startswith(word, position):
                position += len(word)
                return value
        match = NUMBER.match(source, position)
        if match:
            position += len(match[0])
            number = int(match[0])
            if abs(number) <= MAX_INTEGER:
                return number
        raise ProtocolError("invalid number or value")

    try:
        value = parse()
        if position != len(source) or encode(value, max_string=max_string) != data:
            raise ProtocolError("noncanonical encoding")
        return value
    except (IndexError, RecursionError, UnicodeError):
        raise ProtocolError("invalid canonical encoding") from None
