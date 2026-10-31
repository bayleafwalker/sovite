from functools import lru_cache
from importlib import resources
from vuoro_contracts import ContractBundle

PACKAGE = "sovite.repository_repair_contract.vuoro_contract_ffe4db85eaaf3efc0305e243d2cc35cdfcc39879611f606d1282723dc22a79cc_client"
INTEGRITY = '4b83e978ed83be672f4f93cb413aa1500a8ed91bbc8e5866ebf8391ef7fd3e3f'
OPERATION = "repositoryRepairSubmit"
FEEDBACK_CONTEXT_OPERATION = "repositoryRepairFeedbackContext"
RECOVERY_CAPTURE_OPERATION = "repositoryRecoveryCapture"

@lru_cache(maxsize=1)
def load_bundle():
    return ContractBundle.load(resources.files(PACKAGE).joinpath("data"), integrity_sha256=INTEGRITY)

RECOVERY_CONTEXT_OPERATION = "repositoryRecoveryContext"
RECOVERY_RELEASE_OPERATION = "repositoryRecoveryRelease"
