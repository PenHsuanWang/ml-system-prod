"""Outer runtime configuration entry point; never constructs live resources."""

from typing import Any

from src.config.settings import Settings


def load_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)
