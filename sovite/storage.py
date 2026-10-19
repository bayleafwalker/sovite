from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from .db import canonical
from .settings import Settings


class ArtifactStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = Path(settings.artifact_path)
        self.s3 = None
        if settings.artifact_backend == "filesystem":
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            import boto3
            self.s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint, region_name=settings.s3_region)

    @staticmethod
    def check_key(key: str):
        if not re.fullmatch(r"results/[a-zA-Z0-9_-]+/[a-f0-9]{64}\.json", key):
            raise ValueError("invalid private artifact key")

    def put(self, job_id: str, value: dict) -> tuple[str, str]:
        body = canonical(value)
        sha = hashlib.sha256(body).hexdigest()
        key = f"results/{job_id}/{sha}.json"
        self.check_key(key)
        if self.s3:
            self.s3.put_object(Bucket=self.settings.s3_bucket, Key=key, Body=body, ContentType="application/json", Metadata={"sha256": sha})
        else:
            path = self.root / key
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return key, sha

    def get(self, key: str, expected_sha: str) -> bytes:
        self.check_key(key)
        if self.s3:
            body = self.s3.get_object(Bucket=self.settings.s3_bucket, Key=key)["Body"].read()
        else:
            body = (self.root / key).read_bytes()
        if hashlib.sha256(body).hexdigest() != expected_sha:
            raise ValueError("artifact digest mismatch")
        return body

    def health(self) -> bool:
        if self.s3:
            self.s3.head_bucket(Bucket=self.settings.s3_bucket)
        else:
            if not self.root.is_dir() or not os.access(self.root, os.W_OK):
                raise OSError("artifact directory not writable")
        return True
