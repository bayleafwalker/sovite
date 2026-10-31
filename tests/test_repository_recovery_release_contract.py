"""Owner-generated1.3 metadata wire, not a copied request authority schema."""
from importlib import import_module,resources
import hashlib
import json

import httpx
import pytest
from vuoro_contracts import ContractError
from sovite.repository_repair_contract import runtime

BODY={'workspace_id':'workspace-1','attempt_id':'attempt-1','source_digest':'sha256:'+'a'*64,
      'contract_id':'recovery-copy','destination_id':'private-state'}
CONTEXT={'kind':'repository-recovery-artifact','run_id':'run-1','attempt_id':'attempt-1',
         'source_digest':BODY['source_digest'],'contract_id':BODY['contract_id'],
         'contract_revision':'sha256:'+'b'*64,'expected_generation':'sha256:'+'c'*64,
         'procedure_digest':'sha256:'+'d'*64,'output_class':'C3','destinations':['private-state'],
         'publication_eligible':False,'repair_eligible':False}
RELEASE={'release_id':'release_'+'e'*64,'output_class':'C3','output_digest':'sha256:'+'f'*64,
         'contract_revision':CONTEXT['contract_revision'],'destinations':['private-state']}


def sdk(response):
    package=import_module(runtime.PACKAGE);emitted=import_module(runtime.PACKAGE+'.validated_client')
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(200,json=response)
    native=package.Client(base_url='https://owner.invalid')
    native.set_httpx_client(httpx.Client(base_url='https://owner.invalid',transport=httpx.MockTransport(handle)))
    return emitted.create_validated_client(client=native,integrity_sha256=runtime.INTEGRITY),calls


def test_generated_metadata_and_exact_release_selectors_are_the_wire_authority():
    client,calls=sdk(CONTEXT)
    assert client.repository_recovery_context(run_id='run-1',body=BODY).body==CONTEXT
    assert calls[0].url.path=='/api/repository-runs/v1/runs/run-1/recovery-context'
    client,calls=sdk(RELEASE)
    body={**BODY,'expected_generation':CONTEXT['expected_generation'],
          'expected_contract_revision':CONTEXT['contract_revision']}
    assert client.repository_recovery_release(run_id='run-1',body=body).body==RELEASE
    assert calls[0].url.path=='/api/repository-runs/v1/runs/run-1/recovery-release'
    assert json.loads(calls[0].content)==body


@pytest.mark.parametrize('extra',[{'content':'caller bytes'},{'max_bytes':99999999},{'output_class':'C0'},
    {'is_operator':True},{'purpose':'observe'},{'procedure_ref':{}},{'repair_eligible':True}])
def test_generated_requests_cannot_add_content_or_authority(extra):
    client,calls=sdk(CONTEXT)
    with pytest.raises(ContractError):client.repository_recovery_context(run_id='run-1',body={**BODY,**extra})
    assert not calls


@pytest.mark.parametrize('extra',[{'content':'protected bytes'},{'publication_eligible':True},{'repair_eligible':True}])
def test_context_exposes_neither_bytes_nor_execution_eligibility(extra):
    client,calls=sdk({**CONTEXT,**extra})
    with pytest.raises(ContractError):client.repository_recovery_context(run_id='run-1',body=BODY)
    assert len(calls)==1


def test_contract1_3_preserves_every_released1_2_named_definition():
    definitions=json.loads(resources.files(runtime.PACKAGE).joinpath('data/schema/contract.schema.json').read_text())['$defs']
    previous={name:value for name,value in definitions.items()
              if name not in {'RecoveryContextRequest','RecoveryReleaseContext','RecoveryReleaseRequest','RecoveryReleased','NativeActivation'}}
    assert hashlib.sha256(json.dumps(previous,sort_keys=True,separators=(',',':')).encode()).hexdigest()==(
        '15a8847b792a9158272d35c86c1d48eec1e775dc8530ef7d9b2f05702a477f05')


def test_fixed_machine_procedure_document_has_the_published_exact_reference():
    from pathlib import Path
    from sovite.db import canonical
    from sovite.repository_recovery_release import PROCEDURE,PROCEDURE_REF,PROCEDURE_DIGEST
    source=Path(__file__).resolve().parents[1]/'docs/contracts/repository-recovery-exact-copy-v1.json'
    assert json.loads(source.read_bytes())==PROCEDURE
    assert 'sha256:'+hashlib.sha256(canonical(PROCEDURE)).hexdigest()==PROCEDURE_DIGEST
    assert PROCEDURE_REF['content_digest']==PROCEDURE_DIGEST
