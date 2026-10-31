from functools import lru_cache
from importlib import resources
from vuoro_contracts import ContractBundle

PACKAGE = "sovite.repository_repair_contract.vuoro_contract_ffe4db85eaaf3efc0305e243d2cc35cdfcc39879611f606d1282723dc22a79cc_client"
INTEGRITY = "a239a5c515a8ddf10ba138677957f9145ae6b4d814f1cc30f1144aa830c25d98"
OPERATION = "repositoryRepairSubmit"

@lru_cache(maxsize=1)
def load_bundle():
    return ContractBundle.load(resources.files(PACKAGE).joinpath("data"), integrity_sha256=INTEGRITY)
