class JobError(Exception):
    """Application error without knowledge of HTTP or FastAPI."""


class UnknownTask(JobError):
    pass


class JobNotFound(JobError):
    pass


class QueueFull(JobError):
    pass


class DispatcherUnavailable(JobError):
    pass
