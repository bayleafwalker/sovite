"""Owner capture and disclosure provenance for interrupted repository bytes.

Recovery is evidence on the existing execution Job. It cannot complete that
Job, settle provider usage, mint a new attempt or authorize a repair.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace

from .db import Job, WorkerExecutionLease
from .repository_runs import RepositoryRunError, _AttemptBindingResolver, _ID
from .repository_sessions import RepositorySessionManager, _canonical_json, validate_packet

KIND='repository-recovery-artifact'
_TERMINAL=frozenset({'blocked','failed','cancelled'})
_OUTCOMES=frozenset({'uncertain','interrupted','failed','cancelled'})
_SHA=re.compile(r'^[0-9a-f]{64}$')
_LIMIT=8*1024*1024


def need(value):
    if not value:
        raise RepositoryRunError('repository_recovery_unavailable','The exact recovery artifact is unavailable',409)


def outcome_for(job,attempt_id):
    attempt=next(a for a in job.resolved['repository_run']['attempts'] if a['id']==attempt_id)
    state=attempt.get('state')
    if state in _OUTCOMES:return state
    return 'cancelled' if job.status=='cancelled' else ('uncertain' if job.status=='blocked' else 'failed')


def record_for(job, *, attempt_id=None, source_digest=None):
    run=job.resolved.get('repository_run')
    need(type(run)is dict and job.status in _TERMINAL)
    records=run.get('recoveries',[])
    need(type(records)is list and len(records)<=10)
    matches=[row for row in records if type(row)is dict
        and (attempt_id is None or row.get('attempt_id')==attempt_id)
        and (source_digest is None or 'sha256:'+str(row.get('artifact_sha256'))==source_digest)]
    need(len(matches)==1)
    row=matches[0]
    fields={'kind','attempt_id','packet_sha256','artifact_key','artifact_sha256','outcome'}
    need(set(row)==fields and row['kind']==KIND and row['outcome'] in _OUTCOMES
         and type(row['artifact_sha256'])is str and _SHA.fullmatch(row['artifact_sha256'])
         and row['artifact_key']==f"results/{job.id}/{row['artifact_sha256']}.json")
    attempts=[a for a in run['attempts'] if a['id']==row['attempt_id']]
    need(len(attempts)==1 and attempts[0]['packet_sha256']==row['packet_sha256'])
    return row


def payload_for(platform,job,record):
    raw=platform.store.get(record['artifact_key'],record['artifact_sha256'])
    need(len(raw)<=_LIMIT)
    value=json.loads(raw)
    run=job.resolved['repository_run']
    fields={'schema_version','kind','run_id','attempt_id','workspace_id','repository_id','base_sha',
        'tip_sha','patch_sha256','receipt_projection','receipt_projection_sha256','bundle_sha256','bundle_base64',
        'outcome','publication_eligible','repair_eligible'}
    need(type(value)is dict and set(value)==fields
        and value['schema_version']=='sovite-repository-candidate-export/v1' and value['kind']==KIND
        and value['run_id']==job.id and value['attempt_id']==record['attempt_id']
        and value['outcome']==record['outcome'] and value['publication_eligible'] is False
        and value['repair_eligible'] is False
        and all(value[name]==run[name] for name in ('workspace_id','repository_id','base_sha'))
        and _canonical_json(value)==raw)
    projection=value['receipt_projection']
    need(type(projection)is dict and projection.get('packet_sha256')==record['packet_sha256']
         and projection.get('publication_eligible') is False and projection.get('repair_eligible') is False
         and projection.get('within_deadline') is False
         and all(projection.get(name)==value[name] for name in ('attempt_id','workspace_id','repository_id',
                                                                  'base_sha','tip_sha','patch_sha256'))
         and projection.get('job_id')==job.id
         and hashlib.sha256(_canonical_json(projection)).hexdigest()==value['receipt_projection_sha256'])
    bundle=base64.b64decode(value['bundle_base64'],validate=True)
    need(hashlib.sha256(bundle).hexdigest()==value['bundle_sha256'])
    return value


def source_for_digest(platform,job,source_digest):
    """Existing OutputApproval selects one immutable recovery SHA explicitly."""
    record=record_for(job,source_digest=source_digest)
    payload_for(platform,job,record)
    return SimpleNamespace(id=job.id,artifact_key=record['artifact_key'],
                           artifact_sha256=record['artifact_sha256'])


def _identity(job,attempt_id):
    run=job.resolved['repository_run']
    mutable={'attempts','evidence','recoveries','budget_reservations','active_slots','usage_usd','peak_concurrency'}
    attempt=next(row for row in run['attempts'] if row['id']==attempt_id)
    return _canonical_json({'job_id':job.id,'principal_id':job.caller,'generation':job.generation,
        'task':job.task,'repository_run':{key:value for key,value in run.items() if key not in mutable},
        'attempt':{key:attempt[key] for key in ('id','packet_sha256','fence_sha256','base_sha')}})


class RepositoryRecoveryService:
    def __init__(self,owner):
        self.owner=owner
        self.platform=owner.platform
        self.execution=owner.executor

    def _job(self,session,run_id,attempt_id,principal_id,workspace_id):
        need(bool(self.platform.settings.caller_tokens.get(principal_id)))
        job=session.get(Job,run_id)
        need(job is not None and job.caller==principal_id and job.status in _TERMINAL)
        run=job.resolved.get('repository_run')
        need(type(run)is dict and run.get('principal_id')==principal_id
             and run.get('workspace_id')==workspace_id)
        attempts=[a for a in run.get('attempts',[]) if a.get('id')==attempt_id]
        need(len(attempts)==1 and attempts[0].get('state')!='completed')
        lease=session.get(WorkerExecutionLease,job.id)
        need(not (lease is not None and lease.phase=='started'
             and lease.lease_until is not None and lease.lease_until>time.time()))
        need(job.lease_until is None or job.lease_until<=time.time())
        self.platform.authorize_existing_job(session,job,purpose='observe')
        self.execution.check_authorization(job,purpose='observe')
        return job

    @staticmethod
    def _response(job,record,value):
        return {'kind':KIND,'run_id':job.id,'attempt_id':record['attempt_id'],
            'workspace_id':value['workspace_id'],'repository_id':value['repository_id'],
            'base_sha':value['base_sha'],'tip_sha':value['tip_sha'],
            'source_digest':'sha256:'+record['artifact_sha256'],'outcome':record['outcome'],
            'publication_eligible':False,'repair_eligible':False}

    async def capture(self,run_id,attempt_id,*,principal_id,workspace_id):
        try:
            return await self._capture(run_id,attempt_id,principal_id,workspace_id)
        except RepositoryRunError:
            raise
        except Exception:
            raise RepositoryRunError('repository_recovery_unavailable',
                'The exact recovery artifact is unavailable',409) from None

    async def _capture(self,run_id,attempt_id,principal_id,workspace_id):
        need(all(type(value)is str and _ID.fullmatch(value)
                 for value in (run_id,attempt_id,principal_id,workspace_id)))
        expected=None
        def authorized():
            nonlocal expected
            with self.platform.db.sessions() as session:
                current=self._job(session,run_id,attempt_id,principal_id,workspace_id)
                identity=_identity(current,attempt_id)
                if expected is None:expected=identity
                need(identity==expected)
                return current
        job=authorized()
        if any(row.get('attempt_id')==attempt_id for row in job.resolved['repository_run'].get('recoveries',[])):
            record=record_for(job,attempt_id=attempt_id)
            value=payload_for(self.platform,job,record)
            authorized()
            return self._response(job,record,value)
        recover=getattr(self.execution.executor,'recover_attempt',None)
        need(callable(recover))
        run=job.resolved['repository_run']
        identity={'schema_version':'sovite-repository-session/v1','binding_id':run['binding_id'],
            'workspace_id':workspace_id,'repository_id':run['repository_id'],
            'job_id':job.id,'attempt_id':attempt_id}
        key=hashlib.sha256(_canonical_json(identity)).hexdigest()
        manager=RepositorySessionManager(self.execution.state_root,self.execution.resolver)
        lock=manager._lock(key,blocking=False)
        try:
            stored=manager._read_record(manager._record_path(key))
            need(type(stored)is dict and stored.get('attempt_key')==key and stored.get('state')=='prepared')
            packet=validate_packet(stored['packet'])
            attempt=next(a for a in run['attempts'] if a['id']==attempt_id)
            need(stored['packet_sha256']==manager._packet_key(packet)==attempt['packet_sha256']
                 and packet.job_id==job.id and packet.attempt_id==attempt_id
                 and packet.generation==job.generation and packet.base_sha==run['base_sha']
                 and packet.binding_id==run['binding_id'] and packet.workspace_id==workspace_id
                 and packet.repository_id==run['repository_id'] and packet.deadline==run['deadline']
                 and list(packet.allowed_areas)==run['allowed_areas']
                 and hashlib.sha256(packet.fence.encode()).hexdigest()==attempt['fence_sha256']
                 and stored['mirror_root']==run['mirror_root']
                 and stored['worktree']==str(manager._worktree_path(key)))
            manager.binding_resolver=_AttemptBindingResolver({**packet.as_dict(),'mirror_root':run['mirror_root']})
            manager._verify_worktree(manager._worktree_path(key),Path(run['mirror_root']))
            value=await recover(job=job,packet=packet,worktree=manager._worktree_path(key),
                                authorization_check=authorized)
            need(value.get('kind')==KIND)
            artifact_key,sha=self.platform.store.put(job.id,value)
            outcome=outcome_for(job,attempt_id)
            record={'kind':KIND,'attempt_id':attempt_id,'packet_sha256':attempt['packet_sha256'],
                    'artifact_key':artifact_key,'artifact_sha256':sha,'outcome':outcome}
            with self.platform.db.sessions.begin() as session:
                self.platform.db.authority_lock(session)
                current=self._job(session,run_id,attempt_id,principal_id,workspace_id)
                need(_identity(current,attempt_id)==expected)
                resolved=copy.deepcopy(current.resolved)
                records=resolved['repository_run'].setdefault('recoveries',[])
                prior=[row for row in records if row['attempt_id']==attempt_id]
                need(not prior or prior==[record])
                if not prior:
                    records.append(record)
                    resolved['repository_run']['evidence'].append({'label':'recovery-candidate',
                        'uri':f'sovite://repository-runs/{job.id}/{attempt_id}/recovery','sha256':sha})
                    current.resolved=resolved
                verified=record_for(current,attempt_id=attempt_id)
                payload_for(self.platform,current,verified)
                self.platform.db.audit(session,principal_id,'repository_candidate.recovered',job.id,
                    {'attempt_id':attempt_id,'sha256':sha,'outcome':outcome})
                return self._response(current,verified,value)
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            os.close(lock)
