"""Actual generated recovery SDK boundary; it grants no recovery authority."""
from copy import deepcopy
from importlib import import_module, resources
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from vuoro_contracts import ContractError

from sovite.repository_repair_contract import runtime


ROOT=Path(__file__).resolve().parents[1]
REQUEST={'workspace_id':'workspace-1','attempt_id':'attempt-1'}
RESULT={'kind':'repository-recovery-artifact','run_id':'run-1','attempt_id':'attempt-1',
    'workspace_id':'workspace-1','repository_id':'repository-1','base_sha':'a'*40,'tip_sha':'b'*40,
    'source_digest':'sha256:'+'c'*64,'outcome':'interrupted',
    'publication_eligible':False,'repair_eligible':False}


def sdk(response=RESULT,status=200):
    package=import_module(runtime.PACKAGE)
    emitted=import_module(runtime.PACKAGE+'.validated_client')
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(status,json=response)
    native=package.Client(base_url='https://owner.invalid')
    native.set_httpx_client(httpx.Client(base_url='https://owner.invalid',transport=httpx.MockTransport(handle)))
    return emitted.create_validated_client(client=native,integrity_sha256=runtime.INTEGRITY),calls


def test_actual_generated_client_sends_closed_selectors_and_validates_metadata():
    client,calls=sdk()
    answer=client.repository_recovery_capture(run_id='run-1',body=deepcopy(REQUEST))
    assert answer.status==200 and answer.body==RESULT
    assert len(calls)==1 and calls[0].method=='POST'
    assert calls[0].url.path=='/api/repository-runs/v1/runs/run-1/recovery'
    assert json.loads(calls[0].content)==REQUEST
    operation=runtime.load_bundle().manifest['operations'][runtime.RECOVERY_CAPTURE_OPERATION]
    assert operation['exposure']=='internal' and operation['requestMaxBytes']==4096
    assert operation['parameters']==[{'in':'path','name':'run_id','required':True,'schema':'Identity.json'}]


@pytest.mark.parametrize('extra',[{'goal':'replace goal'},{'deadline':'2099-01-01T00:00:00Z'},
    {'publication_eligible':True},{'repair_eligible':True},{'run_id':'different-run'}])
def test_generated_request_cannot_add_authority_or_override_path(extra):
    client,calls=sdk()
    with pytest.raises(ContractError):
        client.repository_recovery_capture(run_id='run-1',body={**REQUEST,**extra})
    assert not calls


@pytest.mark.parametrize('run_id',['run/other','../run','run%2Fother','x'*129])
def test_generated_path_has_exact_owner_identity_boundary(run_id):
    client,calls=sdk()
    with pytest.raises(ContractError): client.repository_recovery_capture(run_id=run_id,body=deepcopy(REQUEST))
    assert not calls


@pytest.mark.parametrize('change',[{'publication_eligible':True},{'repair_eligible':True},
    {'kind':'repository-candidate-export'},{'outcome':'succeeded'},{'base_sha':'a'*39},
    {'source_digest':'c'*64},{'artifact':'raw retained bytes'},{'new_deadline':'2099-01-01'}])
def test_generated_response_never_promotes_interrupted_artifact_or_returns_bytes(change):
    client,calls=sdk({**RESULT,**change})
    with pytest.raises(ContractError): client.repository_recovery_capture(run_id='run-1',body=deepcopy(REQUEST))
    assert len(calls)==1


def test_generated_capture_success_is_200_only():
    client,_=sdk(status=201)
    with pytest.raises(ContractError): client.repository_recovery_capture(run_id='run-1',body=deepcopy(REQUEST))


def test_recovery_source_and_generated_inventory_match_exact_owner_provenance():
    provenance=json.loads((ROOT/'docs/contracts/repository-repair-v1.provenance.json').read_text())
    for name,expected in provenance['sourceSha256'].items():
        assert hashlib.sha256((ROOT/'contracts/repository-repair-v1'/name).read_bytes()).hexdigest()==expected
    package=Path(str(resources.files(runtime.PACKAGE)))
    inventory={p.relative_to(package).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(package.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    actual=hashlib.sha256(json.dumps(inventory,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert actual==provenance['generatedSdk']['treeSha256']
    assert hashlib.sha256((package/'data/integrity.json').read_bytes()).hexdigest()==runtime.INTEGRITY
    assert runtime.load_bundle().manifest['contractVersion']=='1.4.0'


def test_additive_recovery_keeps_all_released_1_1_named_schemas_unchanged():
    # Digest of the actual preceding owner-generated20definitions. No copied
    # DTO or parallel schema is admitted by this compatibility regression.
    package=resources.files(runtime.PACKAGE)
    definitions=json.loads(package.joinpath('data/schema/contract.schema.json').read_text())['$defs']
    previous={name:value for name,value in definitions.items()
        if name not in {'RecoveryCaptureRequest','RecoveryCaptured','GitSha','RecoveryContextRequest','RecoveryReleaseContext',
                        'RecoveryReleaseRequest','RecoveryReleased','NativeActivation'}}
    digest=hashlib.sha256(json.dumps(previous,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert digest=='a21feb2b5332dac3a1c63175241c91305ba0a420c88af977ba491bf68cfef13e'
