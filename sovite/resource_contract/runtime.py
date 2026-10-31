"""Integrity-pinned owner resource schemas and actual generated transport."""
from functools import lru_cache
from importlib import import_module, resources

PACKAGE = 'sovite.resource_contract.vuoro_contract_62061d06ffdbd93279788f4478808b0e5f184642ac0c209f7dcbf462fc8ac33d_client'
INTEGRITY = 'e49351db7ccd082e01a275df0be7a8200896d1faed57827f03081988ea33099c'
OPERATIONS = {"scope":"resourceScopeInstall","output_grant":"resourceOutputGrant",
    "submit":"resourceSubmit","poll":"resourcePoll","current":"resourceCurrent",
    "result":"resourceResult","result/read":"resourceResultRead"}

@lru_cache(maxsize=1)
def load_bundle():
    from vuoro_contracts import ContractBundle
    return ContractBundle.load(resources.files(PACKAGE).joinpath("data"),integrity_sha256=INTEGRITY)

def validate_request(operation,value):
    return load_bundle().validate_request(OPERATIONS[operation],value)

def validate_response(operation,status,value):
    return load_bundle().validate_response(OPERATIONS[operation],status,value)

def validate_schema(name,value):
    return load_bundle().validate_schema(name+".json",value)

def create_validated_client(client):
    return import_module(PACKAGE+".validated_client").create_validated_client(client=client,integrity_sha256=INTEGRITY)
