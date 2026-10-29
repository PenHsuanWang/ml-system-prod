"""Runtime environment names; independent of application-specific configuration."""

from typing import Literal, TypeAlias

Environment: TypeAlias = Literal["development", "test", "production"]
