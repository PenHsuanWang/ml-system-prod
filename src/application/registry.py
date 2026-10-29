from src.domain.jobs.errors import UnknownTask
from src.domain.jobs.ports import TaskHandler


class TaskRegistry:
    """Explicit allow-list of handlers; never dynamically import a client-supplied name."""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, name: str, handler: TaskHandler) -> None:
        if name in self._handlers:
            raise ValueError(f"Task already registered: {name}")
        self._handlers[name] = handler

    def resolve(self, name: str) -> TaskHandler:
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise UnknownTask(f"Unknown task: {name}") from exc
