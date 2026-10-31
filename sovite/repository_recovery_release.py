"""Existing output approval authority applied to an exact stored recovery copy.

The fixed procedure grants no reader/approver authority. Existing policy must
select its exact revision and an enabled caller; no human review is asserted.
"""
from __future__ import annotations

import hashlib

from .db import canonical
from .service import PlatformError

MAX_BYTES=8*1024*1024
PROCEDURE={
    'schema_version':'sovite-repository-recovery-exact-copy/v1',
    'source_kind':'repository-recovery-artifact',
    'operation':'copy-exact-canonical-utf8-json',
    'maximum_bytes':MAX_BYTES,
    'output_class':'same-as-actual-source',
    'authority':'current-existing-output-contract-approver-and-destinations',
    'publication_eligible':False,
    'repair_eligible':False,
}
PROCEDURE_DIGEST='sha256:'+hashlib.sha256(canonical(PROCEDURE)).hexdigest()
PROCEDURE_REF={'owner':'sovite','resource_id':'repository-recovery-exact-copy',
               'revision':'v1','content_digest':PROCEDURE_DIGEST}


def need(value):
    if not value:
        raise PlatformError('recovery_copy_denied','The exact stored recovery copy is unavailable under this procedure',409)


def is_copy_contract(contract):
    return contract.get('procedure_ref')==PROCEDURE_REF


def verify_copy(platform,session,approved,contract,content=None):
    """Current ordinary approval and release reads share this strict procedure."""
    from . import output_contracts as outputs
    from .repository_recovery import record_for,payload_for
    need(is_copy_contract(contract))
    need(approved['actor_role']=='caller')
    job=platform.get_job(approved['source_job_id'],session)
    need(job.caller==approved['actor'])
    record=record_for(job,source_digest=approved['source_digest'])
    value=payload_for(platform,job,record)
    _,recipe=outputs._source(platform,session,job.id,approved['generation'],
                             source_digest=approved['source_digest'])
    need(contract['output_class']==recipe['grant']['output_class'])
    raw=canonical(value)
    need(len(raw)<=MAX_BYTES and hashlib.sha256(raw).hexdigest()==record['artifact_sha256'])
    exact=raw.decode('utf-8')
    if content is not None:need(content==exact)
    platform.authorize_existing_job(session,job,purpose='observe')
    outputs._require_approver(platform,contract,approved['actor'],approved['actor_role'])
    return exact


class RecoveryReleaseService:
    def __init__(self,owner):
        self.owner=owner
        self.platform=owner.platform

    def _context(self,session,run_id,body,principal_id):
        from . import output_contracts as outputs
        from .flow import contract_revision,check_destination
        from .repository_recovery import RepositoryRecoveryService,record_for
        platform=self.platform
        job=RepositoryRecoveryService(self.owner)._job(session,run_id,body['attempt_id'],principal_id,body['workspace_id'])
        record_for(job,attempt_id=body['attempt_id'],source_digest=body['source_digest'])
        bundle,generation=platform.active(session)
        contract=next((item for item in bundle.get('output_contracts',[]) if item['id']==body['contract_id']),None)
        need(contract is not None and is_copy_contract(contract))
        outputs._require_approver(platform,contract,principal_id,'caller')
        need(body['destination_id'] in contract['destinations'])
        destination=next((item for item in bundle['destinations'] if item['id']==body['destination_id']),None)
        need(destination is not None)
        check_destination(bundle,destination,contract['output_class'])
        approved={'source_job_id':job.id,'source_digest':body['source_digest'],'generation':generation,
                  'actor':principal_id,'actor_role':'caller'}
        content=verify_copy(platform,session,approved,contract)
        context={'kind':'repository-recovery-artifact','run_id':job.id,'attempt_id':body['attempt_id'],
            'source_digest':body['source_digest'],'contract_id':contract['id'],
            'contract_revision':contract_revision(contract),'expected_generation':generation,
            'procedure_digest':PROCEDURE_DIGEST,'output_class':contract['output_class'],
            'destinations':contract['destinations'],'publication_eligible':False,'repair_eligible':False}
        from .repository_repair_contract.runtime import load_bundle,RECOVERY_CONTEXT_OPERATION
        from vuoro_contracts import ContractError
        try:load_bundle().validate_response(RECOVERY_CONTEXT_OPERATION,200,context)
        except ContractError:
            raise PlatformError('recovery_copy_unavailable','The current recovery copy metadata is unavailable',503) from None
        # Settings, retained input and installed owner binding are checked after
        # storage I/O as well; Work execution currentness is intentionally absent.
        outputs._require_approver(platform,contract,principal_id,'caller')
        RepositoryRecoveryService(self.owner)._job(session,run_id,body['attempt_id'],principal_id,body['workspace_id'])
        return context,content

    def context(self,run_id,body,*,principal_id):
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            context,_=self._context(session,run_id,body,principal_id)
            return context

    def release(self,run_id,body,*,principal_id):
        from .output_contracts import _approve_output
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            context,content=self._context(session,run_id,body,principal_id)
            need(body['expected_generation']==context['expected_generation']
                 and body['expected_contract_revision']==context['contract_revision'])
        # This deterministic reference records the machine's exact byte check,
        # not a model claim or a fabricated human review. Approval rechecks the
        # current source/procedure/actor under its own authority transaction.
        evidence={'owner':'sovite','resource_id':'repository-recovery-copy:'+body['attempt_id'],
                  'revision':body['source_digest'],'content_digest':body['source_digest']}
        return _approve_output(self.platform,source_job_id=run_id,source_digest=body['source_digest'],
            contract_id=body['contract_id'],expected_contract_revision=body['expected_contract_revision'],
            expected_generation=body['expected_generation'],content=content,evidence_refs=[evidence],
            actor=principal_id,source_copy=True)
