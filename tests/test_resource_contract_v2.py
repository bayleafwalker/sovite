"""Owned TypeSpec provenance and actual emitted SDK against the owner API."""
import base64
import hashlib
from importlib import import_module
import json
from pathlib import Path
import sys

import pytest

from sovite.resource_contract import runtime as contract
from sovite.resource_gateway_store import canonical
from test_execution_leases import owner
from test_migrations import database
from test_policy_v1alpha2 import bundle as gateway_bundle
from test_resource_owner_v2 import OPERATOR,POLICY_REVISION,TOKEN,WORKSPACE,DOMAIN,health,setup

pytestmark=pytest.mark.skipif(sys.version_info<(3,12),reason='Released owner contract runtime requires Python3.12')
ROOT=Path(__file__).resolve().parents[1]


def test_checked_in_sdk_and_sources_match_exact_generator_provenance():
    provenance=json.loads((ROOT/'docs/contracts/resource-owner-v2.provenance.json').read_text())
    assert provenance['generator']['sourceCommit']=='4daf6d17797fb66d6f2e5dc3ab078636f91b67b4'
    for name,digest in provenance['sourceSha256'].items():
        assert hashlib.sha256((ROOT/'contracts/resource-owner-v2'/name).read_bytes()).hexdigest()==digest
    package=ROOT.joinpath(*contract.PACKAGE.split('.'))
    inventory={path.relative_to(package).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob('*')) if path.is_file() and '__pycache__' not in path.parts}
    assert hashlib.sha256(json.dumps(inventory,sort_keys=True,separators=(',',':')).encode()).hexdigest()==provenance['generatedSdk']['treeSha256']
    assert hashlib.sha256((package/'data/integrity.json').read_bytes()).hexdigest()==contract.INTEGRITY==provenance['generatedSdk']['dataIntegritySha256']
    bundle=contract.load_bundle()
    assert len(bundle.manifest['operations'])==7
    assert {item['path'] for item in bundle.manifest['operations'].values()}=={
        '/internal/resource-owner/v2/'+suffix for suffix in
        ('workload-scopes','output-grants','requests','poll','current','result','result/read')}


def test_actual_generated_sdk_all_seven_operations_and_closed_headers(owner):
    f=setup(owner)
    native=import_module(contract.PACKAGE+'.client').Client(base_url='http://testserver').set_httpx_client(f['client'])
    sdk=contract.create_validated_client(native)
    assert sdk.resource_scope_install(Authorization='Bearer '+OPERATOR,body=f['scope']).status==200
    response=sdk.resource_submit(Authorization='Bearer '+TOKEN,body={'execution':f['execution'],
        'resource':{'operation':'model-peer.health-read','target_id':'synthetic-daemon'}})
    assert response.status==200;selected=response.body
    granted=sdk.resource_output_grant(Authorization='Bearer '+OPERATOR,body={
        'workload_id':'actual-workload','principal_id':'operations','tenant_id':f['scope']['tenant_id'],
        'workspace_id':WORKSPACE,'domain_id':DOMAIN,'target_id':'synthetic-daemon',
        'operation':'model-peer.health-read','policy_revision':POLICY_REVISION})
    assert granted.status==200
    auth=f['gateway']['Authorization']
    assert sdk.resource_poll(Authorization=auth,body={}).body['requests'][0]['request_id']==selected['request_id']
    binding={'request_id':selected['request_id'],'fence_fingerprint':selected['fence_fingerprint'],
        'policy_revision':POLICY_REVISION,'destination':'private-state','actor':'operations'}
    assert sdk.resource_current(Authorization=auth,body=binding).body=={'current':True}
    raw=canonical(health())
    assert sdk.resource_result(Authorization=auth,body={**binding,'observation_digest':'sha256:'+hashlib.sha256(raw).hexdigest(),
        'observation_b64':base64.b64encode(raw).decode(),'release':'allowed'}).body=={'accepted':True,'state':'stored'}
    assert sdk.resource_result_read(Authorization='Bearer '+TOKEN,body={
        'execution':f['execution'],'request_id':selected['request_id']}).body['observation']==health()
    from vuoro_contracts import ContractError
    with pytest.raises(ContractError):sdk.resource_submit(Authorization='Bearer '+TOKEN,body={
        'execution':f['execution'],'resource':{'operation':'model-peer.health-read','target_id':'synthetic-daemon','url':'http://other'}})
