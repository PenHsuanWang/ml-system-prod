"""Worker entry point used only by the opt-in integration test."""

from bootstrap.worker import build_worker_broker
from src.config.settings import Settings
from tests.fixtures.neutral_module import module

broker = build_worker_broker(Settings(), modules=(module,))
