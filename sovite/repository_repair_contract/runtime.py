from functools import lru_cache
from importlib import resources
from vuoro_contracts import ContractBundle

PACKAGE = "sovite.repository_repair_contract.vuoro_contract_ffe4db85eaaf3efc0305e243d2cc35cdfcc39879611f606d1282723dc22a79cc_client"
INTEGRITY = '4cae80913a14417b45856b7aa4513c80b049a01aad2f59e757a495ca4f5a5b82'
OPERATION = "repositoryRepairSubmit"
FEEDBACK_CONTEXT_OPERATION = "repositoryRepairFeedbackContext"
RECOVERY_CAPTURE_OPERATION = "repositoryRecoveryCapture"

@lru_cache(maxsize=1)
def load_bundle():
    return ContractBundle.load(resources.files(PACKAGE).joinpath("data"), integrity_sha256=INTEGRITY)

RECOVERY_CONTEXT_OPERATION = "repositoryRecoveryContext"
RECOVERY_RELEASE_OPERATION = "repositoryRecoveryRelease"

NATIVE_ACTIVATION_OPERATION = "repositoryNativeActivationRead"
