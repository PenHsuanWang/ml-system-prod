"""Metadata admission and background application orchestration for DatasetBuilder."""

from collections.abc import Callable
from threading import Event, Thread
from typing import Protocol

from src.application.datasets.builder import EBMDatasetPreparationService, approve_build_inputs
from src.application.datasets.ports import PublishedRawSnapshotCatalog
from src.domain.datasets.build_jobs import (
    BuildState,
    DatasetBuildClaim,
    DatasetBuildJob,
    DatasetBuildSpec,
)
from src.domain.ml import (
    Actor,
    Conflict,
    Forbidden,
    MLError,
    NotFound,
    SchemaMismatch,
    TransientDependency,
    Unavailable,
    digest,
)
from src.domain.pagination import CollectionWindow


class BuildJobCatalog(Protocol):
    def submit(
        self, actor: Actor, key: str, spec: DatasetBuildSpec, raw: str, spec_digest: str
    ) -> DatasetBuildJob: ...
    def get(self, actor: Actor, job_id: str) -> DatasetBuildJob: ...
    def list_jobs(
        self, actor: Actor, window: CollectionWindow, state: BuildState | None
    ) -> list[DatasetBuildJob]: ...


class BuildWorkQueue(Protocol):
    def claim(self, job_id: str | None = None) -> DatasetBuildClaim | None: ...
    def renew(self, claim: DatasetBuildClaim) -> bool: ...
    def fail(self, claim: DatasetBuildClaim, error_code: str, *, retry: bool = False) -> None: ...


class DatasetBuildService:
    def __init__(self, jobs: BuildJobCatalog, snapshots: PublishedRawSnapshotCatalog) -> None:
        self.jobs, self.snapshots = jobs, snapshots

    def submit(self, actor: Actor, key: str, spec: DatasetBuildSpec) -> DatasetBuildJob:
        actor.require("ml:dataset:manage")
        if not 1 <= len(key) <= 128 or any(not 33 <= ord(char) <= 126 for char in key):
            raise SchemaMismatch("Idempotency-Key must be 1..128 visible ASCII characters")
        approve_build_inputs(self.snapshots, actor, spec.inputs)
        raw = spec.to_json()
        return self.jobs.submit(actor, key, spec, raw, digest(["dataset-build-spec-v1", raw]))

    def get(self, actor: Actor, job_id: str) -> DatasetBuildJob:
        return self.jobs.get(actor, job_id)

    def page(
        self, actor: Actor, window: CollectionWindow, state: BuildState | None
    ) -> tuple[list[DatasetBuildJob], int | None]:
        rows = self.jobs.list_jobs(actor, window, state)
        return rows[: window.limit], window.next_offset(has_more=len(rows) > window.limit)


class DatasetBuildWorker:
    def __init__(
        self,
        queue: BuildWorkQueue,
        preparation: Callable[[DatasetBuildClaim], EBMDatasetPreparationService],
        resolve_actor: Callable[[str, str], Actor],
        *,
        heartbeat_interval: float = 60,
    ) -> None:
        self.queue, self.preparation, self.resolve_actor = queue, preparation, resolve_actor
        self.heartbeat_interval = heartbeat_interval

    def run_once(self, job_id: str | None = None) -> str | None:
        claim = self.queue.claim(job_id)
        if claim is None:
            return None
        job = claim.job
        stopped, lost = Event(), Event()

        def renew_lease() -> None:
            while not stopped.wait(self.heartbeat_interval):
                try:
                    if not self.queue.renew(claim):
                        lost.set()
                        return
                except TransientDependency:
                    # The last durable lease still governs ownership.
                    continue
                except Exception:
                    # Stop renewal and fence publication without leaking driver details.
                    lost.set()
                    return

        def authorize() -> Actor:
            if lost.is_set():
                raise Conflict("Dataset build lease renewal stopped")
            current = self.resolve_actor(job.tenant_id, job.owner_id)
            if (current.tenant_id, current.actor_id) != (job.tenant_id, job.owner_id):
                raise Forbidden("DatasetBuilder principal changed")
            return current

        heartbeat = Thread(target=renew_lease, name="dataset-build-lease", daemon=True)
        heartbeat.start()
        try:
            spec = DatasetBuildSpec.verified(job.spec_json, job.spec_digest)
            self.preparation(claim).build(
                authorize(),
                spec.dataset_id,
                spec.version,
                spec.inputs,
                spec.config,
                authorize_publication=authorize,
            )
        except TransientDependency:
            # If publication committed before a disconnect, this fenced write is a no-op.
            self.queue.fail(claim, "BUILD_DEPENDENCY_UNAVAILABLE", retry=True)
        except Unavailable:
            self.queue.fail(claim, "BUILD_DEPENDENCY_UNAVAILABLE")
        except (Forbidden, NotFound):
            self.queue.fail(claim, "BUILD_INPUT_ACCESS_DENIED")
        except MLError:
            self.queue.fail(claim, "BUILD_REJECTED")
        except Exception:
            # Persist a safe code. Private source paths and adapter errors stay out of HTTP.
            self.queue.fail(claim, "BUILD_FAILED")
        finally:
            stopped.set()
            heartbeat.join()
        return job.job_id
