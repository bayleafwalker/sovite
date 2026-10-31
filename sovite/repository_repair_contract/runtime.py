from functools import lru_cache
from importlib import resources
from vuoro_contracts import ContractBundle

PACKAGE = "sovite.repository_repair_contract.vuoro_contract_ffe4db85eaaf3efc0305e243d2cc35cdfcc39879611f606d1282723dc22a79cc_client"
INTEGRITY = "3690da2ebd4fb8ffb5ddac04749e2cb1a191f3821fe4b1e466b95e01426d94e2"
OPERATION = "repositoryRepairSubmit"
FEEDBACK_CONTEXT_OPERATION = "repositoryRepairFeedbackContext"

@lru_cache(maxsize=1)
def load_bundle():
    return ContractBundle.load(resources.files(PACKAGE).joinpath("data"), integrity_sha256=INTEGRITY)
