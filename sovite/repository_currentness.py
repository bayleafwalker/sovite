"""Optional installed veto of active repository work, separate from data access.

The grant owner declares the requirement in published policy. An installed
application supplies one fixed authorizer object; requests cannot select an
implementation, endpoint, purpose or authority. Owner selectors remain opaque to
Sovite and live only in existing Job repository metadata, never another ledger.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Protocol

SCHEMA='sovite-repository-currentness/v1'
FIELD='project_currentness'
_LIMIT=16*1024
_ERRORS={
    'repository_currentness_unavailable':(503,'Repository work authority is unavailable'),
    'repository_currentness_revoked':(409,'Repository work authority is no longer current'),
    'repository_currentness_binding_invalid':(409,'Repository work binding is invalid'),
    'repository_currentness_required':(409,'Repository work authority is required'),
}


class CurrentnessError(ValueError):
    """Static public refusal; never include owner payloads, URLs or credentials."""
    def __init__(self,code='repository_currentness_unavailable'):
        if type(code) is not str or code not in _ERRORS:code='repository_currentness_unavailable'
        self.code=code;self.status,self.message=_ERRORS[code]
        super().__init__(self.message)


class CurrentnessAuthorizer(Protocol):
    def admit(self,*,repository_run:dict,task:str,caller:str,recipe:dict,parent)->dict:...
    def bind_job(self,*,job,admission:dict)->dict:...
    def check_job(self,*,job,binding:dict,authorization_started_at:float)->None:...


def _need(condition,code='repository_currentness_binding_invalid'):
    if not condition:raise CurrentnessError(code)


def _opaque(value):
    _need(type(value) is dict)
    try:
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
        _need(len(raw)<=_LIMIT)
        return json.loads(raw)
    except (TypeError,ValueError,UnicodeError,RecursionError):
        raise CurrentnessError('repository_currentness_binding_invalid') from None


def _packet(job):
    from .repository_runs import validate_run_packet,canonical_packet_digest
    try:
        run=job.resolved['repository_run']
        _need(type(run) is dict and run.get('schema_version')=='sovite-repository-run/v1')
        packet=validate_run_packet({'schema_version':'portable-work/v1',
            **{key:run[key] for key in ('workspace_id','repository_id','executor_id','base_sha',
                                       'max_cost_usd','deadline','concurrency')},'goal':job.task})
        checksum=canonical_packet_digest(packet)
        _need(checksum==run['packet_sha256'] and run['principal_id']==job.caller
              and run['grant_id']==job.grant_id and run['generation']==job.generation)
        return checksum
    except CurrentnessError:raise
    except Exception:
        raise CurrentnessError('repository_currentness_binding_invalid') from None


def _binding(job):
    try:
        value=job.resolved['repository_run'][FIELD]
        _need(type(value) is dict and set(value)=={'schema_version','job_id','principal_id','packet_sha256','binding'})
        _need(value['schema_version']==SCHEMA and value['job_id']==job.id
              and value['principal_id']==job.caller and value['packet_sha256']==_packet(job))
        return _opaque(value['binding'])
    except CurrentnessError:raise
    except Exception:
        raise CurrentnessError('repository_currentness_binding_invalid') from None


def _hook(platform):
    try:
        hook=getattr(platform,'repository_currentness_authorizer',None)
        _need(hook is not None and all(callable(getattr(hook,name,None)) for name in
              ('admit','bind_job','check_job')),'repository_currentness_unavailable')
        return hook
    except CurrentnessError:raise
    except Exception:
        raise CurrentnessError('repository_currentness_unavailable') from None


def _invoke(method,**kwargs):
    try:return method(**kwargs)
    except CurrentnessError:raise
    except Exception:
        raise CurrentnessError('repository_currentness_unavailable') from None


def required(recipe):
    value=recipe['grant'].get('repository_currentness_required',False)
    _need(type(value) is bool)
    return value


def check_installed(platform,recipe):
    """Local composition check only; capability discovery never reads Work."""
    if required(recipe):_hook(platform)


def admit(platform,session,*,repository_run,task,caller,recipe):
    """Select protected owner scope only after actual grant/repair admission.

    No timestamp is retained or added to idempotency. The actual authorizer owns
    fresh remote reads and stable selector validation. Cross-owner reads and the
    local commit are not claimed to be an atomic network transaction.
    """
    parent=None
    if repository_run is not None and 'repair' in repository_run:
        from .db import Job
        parent=session.get(Job,repository_run['repair']['parent_run_id'])
        _need(parent is not None)
    inherited=parent is not None and FIELD in parent.resolved.get('repository_run',{})
    if not required(recipe) and not inherited:return None
    _need(repository_run is not None,'repository_currentness_required')
    _packet(SimpleNamespace(resolved={'repository_run':repository_run},task=task,
        caller=caller,grant_id=recipe['grant']['id'],generation=recipe['generation']))
    if inherited:_binding(parent)
    hook=_hook(platform)
    return _opaque(_invoke(hook.admit,repository_run=copy.deepcopy(repository_run),task=task,
        caller=caller,recipe=copy.deepcopy(recipe),parent=parent))


def bind_job(platform,job,admission):
    if admission is None:return
    checksum=_packet(job)
    principal,job_id=job.caller,job.id
    value=_opaque(_invoke(_hook(platform).bind_job,job=job,admission=copy.deepcopy(admission)))
    _need(job.id==job_id and job.caller==principal and _packet(job)==checksum)
    resolved=copy.deepcopy(job.resolved)
    resolved['repository_run'][FIELD]={'schema_version':SCHEMA,'job_id':job_id,
        'principal_id':principal,'packet_sha256':checksum,'binding':value}
    job.resolved=resolved


def check_job(platform,job,recipe,*,authorization_started_at):
    run=job.resolved.get('repository_run')
    bound=type(run) is dict and FIELD in run
    if not required(recipe) and not bound:return
    _need(bound,'repository_currentness_required')
    binding=_binding(job)
    result=_invoke(_hook(platform).check_job,job=job,binding=binding,
                   authorization_started_at=authorization_started_at)
    _need(result is None,'repository_currentness_unavailable')
