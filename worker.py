"""Run: taskiq worker worker:broker --ack-type when_saved --workers 2."""

from bootstrap.config import load_settings
from bootstrap.worker import build_worker_broker

# Topology and handlers are registered here; connections open in worker startup.
broker = build_worker_broker(load_settings())
