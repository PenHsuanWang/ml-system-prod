"""Read-only dataset discovery through the existing DatasetService."""

from typing import Annotated

from fastapi import APIRouter, Query, Response

from src.api.collection_schemas import (
    COLLECTION_RESPONSES,
    CollectionError,
    CollectionQuery,
    MetadataPage,
)
from src.api.datasets.schemas import (
    DatasetFamily,
    DatasetFamilyQuery,
    DatasetKey,
    DatasetLineage,
    DatasetMetadata,
)
from src.api.ml_http import ActorDependency, ServicesDependency

router = APIRouter(tags=["dataset discovery"], responses=COLLECTION_RESPONSES)


@router.get(
    "",
    response_model=MetadataPage[DatasetFamily],
    operation_id="list_dataset_families",
    summary="List accessible dataset families",
    description="Requires ml:train or ml:dataset:read. Includes tenant and public fixture data. "
    "Private versions shadow identical public identities. SQL groups by dataset ID "
    "in ascending order. Version counts and last publication apply to matching time filters. "
    "Default 25/max 100 items; concurrent changes may shift offset pages. "
    "Use /{dataset_id}/versions to choose an immutable training dataset version.",
)
def families(
    response: Response,
    actor: ActorDependency,
    services: ServicesDependency,
    page: Annotated[DatasetFamilyQuery, Query()],
) -> MetadataPage[DatasetFamily]:
    response.headers["Cache-Control"] = "private, no-store"
    return MetadataPage[DatasetFamily].model_validate(
        services.datasets.families(actor, page.window(), page.dataset_id)
    )


@router.get(
    "/{dataset_id}/versions",
    response_model=MetadataPage[DatasetMetadata],
    operation_id="list_dataset_versions",
    summary="List accessible versions of a dataset",
    description="Uses dataset read permissions and tenant/public shadow rules. Order is "
    "lexically ascending; no latest-version or semantic-version interpretation is applied. "
    "Default 25/max 100. Empty/invisible families return an empty page. "
    "Schema, shape, feature configuration and split counts help select a compatible recipe.",
)
def versions(
    response: Response,
    dataset_id: DatasetKey,
    actor: ActorDependency,
    services: ServicesDependency,
    page: Annotated[CollectionQuery, Query()],
) -> MetadataPage[DatasetMetadata]:
    response.headers["Cache-Control"] = "private, no-store"
    return MetadataPage[DatasetMetadata].model_validate(
        services.datasets.versions(actor, dataset_id, page.window())
    )


@router.get(
    "/{dataset_id}/versions/{version}",
    response_model=DatasetMetadata,
    operation_id="get_dataset_version_metadata",
    responses={
        404: {"description": "Unknown or invisible dataset version.", "model": CollectionError}
    },
    summary="Get immutable dataset version metadata",
    description="Requires ml:train or ml:dataset:read within dataset visibility. Unknown/invisible "
    "versions return 404. Returns safe schema, splits, content identity and publication time. "
    "The existing /versions/{dataset_id}@{version} lookup remains supported.",
)
def version_metadata(
    response: Response,
    dataset_id: DatasetKey,
    version: DatasetKey,
    actor: ActorDependency,
    services: ServicesDependency,
) -> DatasetMetadata:
    response.headers["Cache-Control"] = "private, no-store"
    return DatasetMetadata.model_validate(services.datasets.metadata(actor, dataset_id, version))


@router.get(
    "/{dataset_id}/versions/{version}/lineage",
    response_model=DatasetLineage,
    operation_id="get_dataset_version_lineage",
    responses={
        404: {"description": "Unknown or invisible dataset version.", "model": CollectionError}
    },
    summary="Trace a dataset to verified published inputs",
    description="Requires dataset read permission. Complete receipts also require ingestion read "
    "permission and access to every input under owner/operate rules. Restricted inputs "
    "withhold the receipt and source identifiers. Legacy missing or corrupt evidence is explicitly "
    "incomplete. Complete receipts bind dataset content, builder/version/config and source "
    "revision/digests. SHA-256 of the canonical receipt matches receipt_digest. "
    "No raw files are read or downloaded by this API.",
)
def lineage(
    response: Response,
    dataset_id: DatasetKey,
    version: DatasetKey,
    actor: ActorDependency,
    services: ServicesDependency,
) -> DatasetLineage:
    response.headers["Cache-Control"] = "private, no-store"
    return DatasetLineage.model_validate(services.datasets.lineage(actor, dataset_id, version))
