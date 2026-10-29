import re
import tempfile
from pathlib import Path

from src.domain.ml import MLError
from src.infrastructure.serving.artifacts import ArtifactVerifier


class BundleMaterializer:
    def __init__(self, local_root: Path, tracking_uri: str) -> None:
        self.local_root = local_root.resolve()
        self.tracking_uri = tracking_uri

    def materialize(self, artifact_ref: str, expected_digest: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None:
            raise MLError("Invalid manifest digest")
        if artifact_ref.startswith("runs:/"):
            match = re.fullmatch(r"runs:/([a-f0-9]{32})/bundle", artifact_ref)
            if match is None:
                raise MLError("Unapproved run artifact reference")
            from mlflow import MlflowClient
            client = MlflowClient(tracking_uri=self.tracking_uri)
            self.local_root.mkdir(parents=True, exist_ok=True)
            cache = self.local_root/expected_digest
            if not cache.exists():
                with tempfile.TemporaryDirectory(dir=self.local_root) as staging:
                    result = Path(client.download_artifacts(match[1], "bundle", staging))
                    ArtifactVerifier().verify(result, expected_digest)
                    try:
                        result.rename(cache)
                    except FileExistsError:
                        pass
            directory = cache
        else:
            directory = Path(artifact_ref).resolve(strict=True)
            if self.local_root not in directory.parents:
                raise MLError("Artifact is outside the approved store")
        ArtifactVerifier().verify(directory, expected_digest)
        return directory
