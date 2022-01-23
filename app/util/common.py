from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any, Callable, Iterator, Optional, TYPE_CHECKING, Type, TypeVar

from discord.ext.commands import Converter

if TYPE_CHECKING:
    from app.core import Context

    Q = TypeVar('Q')
    T = TypeVar('T')

__all__ = (
    'setinel',
    'level_requirement_for',
    'calculate_level',
    'converter',
)

EMOJI_REGEX: re.Pattern[str] = re.compile(r'<(a)?:([a-zA-Z0-9_]{2,32}):([0-9]{17,25})>')


# This exists for type checkers
class ConstantT:
    pass


def setinel(name: str, **dunders) -> ConstantT:
    attrs = {f'__{k}__': lambda _: v for k, v in dunders.items()}
    return type(name, (ConstantT,), attrs)()


def converter(f: Callable[[Context, str], T]) -> Type[Converter]:
    class Wrapper(Converter):
        async def convert(self, ctx: Context, argument: str) -> T:
            return await f(ctx, argument)

    return Wrapper


def level_requirement_for(level: int, /, *, base: int = 1000, factor: float = 1.45) -> int:
    precise = base * factor ** level
    return math.ceil(precise / 100) * 100


def calculate_level(exp: int, *, base: int = 1000, factor: float = 1.45) -> tuple[int, int, int]:
    kwargs = {'base': base, 'factor': factor}
    level = 0

    while exp > (requirement := level_requirement_for(level, **kwargs)):
        exp -= requirement
        level += 1

    return level, exp, requirement


def image_url_from_emoji(emoji: str) -> str:
    if match := EMOJI_REGEX.match(emoji):
        animated, _, id = match.groups()
        extension = 'gif' if animated else 'png'
        return f'https://cdn.discordapp.com/emojis/{id}.{extension}?v=1'
    else:
        code = format(ord(emoji[0]), 'x')
        return f'https://twemoji.maxcdn.com/v/latest/72x72/{code}.png'


def walk_collection(collection: type, cls: Type[Q]) -> Iterator[Q]:
    for attr in dir(collection):
        if attr.startswith('_'):
            continue

        obj = getattr(collection, attr)
        if not isinstance(obj, cls):
            continue

        yield obj


def get_by_key(collection: type, key: str) -> Any:
    for attr in dir(collection):
        if attr.startswith('_'):
            continue

        obj = getattr(collection, attr)
        if hasattr(obj, 'key') and obj.key == key:
            return obj


def query_collection(collection: type, cls: Type[Q], query: str) -> Optional[Q]:
    query = query.lower()
    queued = []

    for obj in walk_collection(collection, cls):
        query = query.lower()
        name = obj.name.lower()

        if query == name:
            return obj

        if len(query) >= 3 and query in name:
            queued.append(obj)

        matcher = SequenceMatcher(None, query, name)
        if matcher.ratio() > .85:
            queued.append(obj)

    if queued:
        return queued[0]
