"""Load the integrity-pinned opt-in external fairness contract v2."""
from __future__ import annotations

from functools import lru_cache
from importlib import resources

from vuoro_contracts import ContractBundle

CONTRACT_PACKAGE = (
    "sovite.execution_contract."
    "vuoro_contract_61440b32de59136facdd786826aa33c916da1dbb981dc6f3fc2115ce2f952d02_client"
    ".clients.python."
    "vuoro_contract_61440b32de59136facdd786826aa33c916da1dbb981dc6f3fc2115ce2f952d02_client"
)
DATA_INTEGRITY_SHA256 = "4d7110069ba5d57e8af1144bc3a305659aff6e24d1e79e8279ad6fdfcaac8ff5"
OPERATIONS = {"pull": "executionPullV2", "lookup": "executionLookupV2"}


@lru_cache(maxsize=1)
def load_bundle() -> ContractBundle:
    return ContractBundle.load(
        resources.files(CONTRACT_PACKAGE).joinpath("data"),
        integrity_sha256=DATA_INTEGRITY_SHA256,
    )


def validate_request(operation: str, value: dict) -> None:
    load_bundle().validate_request(OPERATIONS[operation], value)


def validate_response(operation: str, status: int, value: dict) -> None:
    load_bundle().validate_response(OPERATIONS[operation], status, value)
