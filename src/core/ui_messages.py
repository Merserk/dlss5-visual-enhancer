"""Translation templates that also remain readable in logs and headless code."""
from __future__ import annotations

import re


def substitute(source: str, args: tuple[str, ...]) -> str:
    return re.sub(r"%([1-9]\d*)", lambda match: args[int(match[1]) - 1], source)


class UiMessage(str):
    """Retain the English diagnostic and arguments until the UI translates it."""

    def __new__(cls, source: str, *args):
        if isinstance(source, cls) and not args:
            return source
        values = tuple(str(arg) for arg in args)
        obj = super().__new__(cls, substitute(source, values))
        obj.source = source
        obj.args = args
        return obj

    def __reduce_ex__(self, protocol):
        return type(self), (self.source, *self.args)


def join_messages(values: list[str], separator: str = "\n") -> UiMessage:
    return UiMessage(separator.join(f"%{index+1}" for index in range(len(values))), *values)
