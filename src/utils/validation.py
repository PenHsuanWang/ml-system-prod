"""Pure validation helpers shared by outer adapters."""

import re

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_valid_request_id(value: str) -> bool:
    return _REQUEST_ID.fullmatch(value) is not None
