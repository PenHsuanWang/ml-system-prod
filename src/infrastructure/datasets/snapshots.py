"""Only registered immutable references reach the child; arbitrary URLs are rejected."""

import hashlib
import json
from pathlib import Path
from collections.abc import Mapping

from src.domain.datasets.models import DatasetSnapshot
from src.domain.ml import MLError


class FileDatasetSource:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self, reference: Mapping[str, str]) -> DatasetSnapshot:
        ref = reference["snapshot_ref"]
        if ref == "builtin:dataset-demo-v1":
            path = Path(__file__).with_name("dataset-demo.json")
        else:
            if Path(ref).is_symlink():
                raise MLError("Dataset symbolic links are forbidden")
            path = Path(ref).resolve(strict=True)
            if self.root not in path.parents or path.is_symlink():
                raise MLError("Dataset is outside its approved store")
        if path.stat().st_size > 16*1024*1024:
            raise MLError("Dataset exceeds the CPU profile size limit")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["content_digest"]:
            raise MLError("Dataset snapshot digest mismatch")
        return DatasetSnapshot.parse(json.loads(data))
