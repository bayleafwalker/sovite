"""Load and validate the admitted agent-http-v1 wire contract."""
from __future__ import annotations

from functools import lru_cache
from importlib import import_module, resources

from vuoro_contracts import ContractBundle

CONTRACT_PACKAGE = "sovite.execution_contract.vuoro_contract_c3c73ae52d117dd8407c15d4b4b549461e753e30de4783134a2cad02f5dd1ab4_client"
DATA_INTEGRITY_SHA256 = "afe312a6a0ed9a446d69a9ca6e9c8b00b2c5574cf3ac852a0ea27912ce74741a"
OPERATIONS = {
    "pull": "executionPull",
    "start": "executionStart",
    "renew": "executionRenew",
    "status": "executionStatus",
    "handback": "executionHandback",
    "complete": "executionComplete",
    "lookup": "executionLookup",
}


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


def create_validated_client(client):
    package = import_module(CONTRACT_PACKAGE + ".validated_client")
    return package.create_validated_client(
        client=client, integrity_sha256=DATA_INTEGRITY_SHA256,
    )
