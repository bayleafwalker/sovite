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
DATA_INTEGRITY_SHA256 = "b136c5bca7c39729ac760b674488cb12e9839b063878e939e1a0eda99a57e45f"
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
