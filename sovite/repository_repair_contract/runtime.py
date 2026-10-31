from functools import lru_cache
from importlib import resources
from vuoro_contracts import ContractBundle

PACKAGE = "sovite.repository_repair_contract.vuoro_contract_ffe4db85eaaf3efc0305e243d2cc35cdfcc39879611f606d1282723dc22a79cc_client"
INTEGRITY = "87f4144ce741e444833ad1ee6a9987108703416a246494b8f93cf8ea6751977f"
OPERATION = "repositoryRepairSubmit"
FEEDBACK_CONTEXT_OPERATION = "repositoryRepairFeedbackContext"
RECOVERY_CAPTURE_OPERATION = "repositoryRecoveryCapture"

@lru_cache(maxsize=1)
def load_bundle():
    return ContractBundle.load(resources.files(PACKAGE).joinpath("data"), integrity_sha256=INTEGRITY)
