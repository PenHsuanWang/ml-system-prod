"""ASGI factory used only by the opt-in multiprocess integration test."""

from fastapi import FastAPI

from server import create_app as create_backend_app
from tests.fixtures.neutral_module import module


def create_app() -> FastAPI:
    return create_backend_app(modules=(module,))
