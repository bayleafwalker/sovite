"""Shared strict request shapes, with no control-store dependencies."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Id = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
Secret = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Start(Strict):
    gateway_id: Id


class InferenceRequest(Strict):
    job_id: Id
    fence: Id
    session_id: Id
    operation: Literal["chat", "responses", "ollama-chat"]
    body_digest: Digest
    body_size: Annotated[int, Field(ge=1, le=4 * 1024 * 1024)]
    deadline: Annotated[int, Field(ge=1, le=2**53 - 1)]


class Authorize(Start):
    request_id: Id
    request: InferenceRequest
    capability: Secret
    workload: Annotated[str, Field(min_length=1, max_length=160)]
