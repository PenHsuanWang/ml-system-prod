"""Classification for verified local and shared filesystem operations."""

import errno
from collections.abc import Iterator
from contextlib import contextmanager

from src.domain.ml import TransientDependency

_TRANSIENT_CODES = {
    errno.EAGAIN,
    errno.EBUSY,
    errno.ECONNRESET,
    errno.EINTR,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ENOTCONN,
    errno.ESTALE,
    errno.ETIMEDOUT,
}


def raise_if_transient_storage_error(error: BaseException | None) -> None:
    if isinstance(error, OSError) and error.errno in _TRANSIENT_CODES:
        raise TransientDependency("Shared storage is temporarily unavailable") from None


@contextmanager
def classify_storage_errors() -> Iterator[None]:
    try:
        yield
    except OSError as error:
        raise_if_transient_storage_error(error)
        raise
