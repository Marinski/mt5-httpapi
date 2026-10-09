"""JSON key naming at the API boundary.

Responses use snake_case keys, the same as the MT5 SDK fields the API passes
through (time_msc, volume_real). The backtest and TA routes used to answer in
camelCase (jobId, startedAt, wickworksStatus), so for now each of those keys
is also sent under its old name. The old names are deprecated and go away in
v5.0.0, as README.md says; removing them means deleting the legacy half of
with_legacy_keys and accept_snake_keys.

Request bodies that took camelCase keys accept the snake_case form too.

Only the API's own field names are converted. Data maps keep their keys as
they are: an EA's input names, optimization result columns, a wickworks
indicator spec.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def to_snake(name: str) -> str:
    """`maxDrawdownAbsolute` -> `max_drawdown_absolute`."""
    return _WORD_BOUNDARY.sub("_", name).lower()


def with_legacy_keys(payload: dict) -> dict:
    """`payload` with snake_case keys, plus each key that was camelCase also
    under its old name. Not recursive: nested API objects are converted by
    the caller, so data maps inside are never touched."""
    converted = {to_snake(key): value for key, value in payload.items()}
    legacy = {key: value for key, value in payload.items() if to_snake(key) != key}
    return {**converted, **legacy}


class ConflictingKeys(ValueError):
    """A request body gave the same field under its camelCase and its
    snake_case name, with different values."""


def accept_snake_keys(body: dict, camel_names: Iterable[str]) -> dict:
    """`body` with the snake_case form of each name in `camel_names` renamed
    to the camelCase name the handler reads. Raises ConflictingKeys when both
    forms are given with different values."""
    normalized = dict(body)
    for camel in camel_names:
        snake = to_snake(camel)
        if snake not in normalized:
            continue
        value = normalized.pop(snake)
        if camel in normalized and normalized[camel] != value:
            raise ConflictingKeys(f"{snake} and {camel} are the same field; send one")
        normalized[camel] = value
    return normalized
