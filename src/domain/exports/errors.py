class ExportError(Exception):
    """An export failure with a message safe for an API client."""


class InvalidExportRequest(ExportError):
    pass


class ExportSourceNotFound(ExportError):
    pass


class ExportUnavailable(ExportError):
    pass


class ExportRowTooLarge(ExportUnavailable):
    pass
