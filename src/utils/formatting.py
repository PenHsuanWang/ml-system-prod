"""Pure serialization helpers; no orchestration or external I/O."""

import json

from src.shared_models.types import JSONValue


def csv_cell(value: JSONValue) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict | list):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    else:
        text = str(value)
    # Leading whitespace can conceal spreadsheet formula prefixes. Escape tabs
    # and CR themselves as well as formulas after any leading whitespace.
    stripped = text.lstrip()
    leading = text[: len(text) - len(stripped)]
    if any(character in leading for character in ("\t", "\r", "\n")) or stripped.startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + text
    return text
