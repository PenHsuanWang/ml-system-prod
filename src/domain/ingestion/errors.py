"""Safe public error categories; adapter exception details never cross the boundary."""

from src.domain.ml import Conflict, MLError


class IngestionError(MLError):
    code = "INGESTION_INVALID"


class SourceRejected(IngestionError):
    code = "SOURCE_REJECTED"


class SourceChanged(IngestionError):
    code = "SOURCE_CHANGED"


class AcquisitionFailed(IngestionError):
    code, retryable = "ACQUISITION_FAILED", True


class AcquisitionDeadline(IngestionError):
    code, retryable = "ACQUISITION_DEADLINE", True


class IngestionCancelled(IngestionError):
    code = "INGESTION_CANCELLED"


class SnapshotConflict(Conflict):
    code = "RAW_SNAPSHOT_CONFLICT"


class SnapshotInvalid(IngestionError):
    code = "RAW_SNAPSHOT_INVALID"


class PublicationUncertain(IngestionError):
    code, retryable = "PUBLICATION_UNCERTAIN", True
