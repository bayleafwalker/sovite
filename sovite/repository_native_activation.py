"""Current owner projection and non-authoritative single-use locator transport."""
from __future__ import annotations
import hashlib,json,math,os,stat,time,uuid
from pathlib import Path
from datetime import datetime
from .adapters import RuntimeFailure
from .db import Job,WorkerExecutionLease
from .repository_runs import RepositoryRunError,RUN_SCHEMA

SCHEMA='sovite-native-activation/v1'
LOCATOR=Path('/var/lib/vuoro-cloud/native-vm-activation/request.json')
FIELDS={'schema_version','job_id','attempt_id','workspace_id','repository_id','principal_id',
        'generation','fence_sha256','packet_sha256','deadline'}


def need(value):
    if not value:raise RuntimeFailure('native_vm_activation_unavailable')


def metadata(context,packet_sha256):
    return {'schema_version':SCHEMA,'job_id':context.job_id,'attempt_id':context.attempt_id,
        'workspace_id':context.workspace_id,'repository_id':context.repository_id,
        'principal_id':context.principal_id,'generation':context.generation,
        'fence_sha256':hashlib.sha256(context.fence.encode()).hexdigest(),
        'packet_sha256':packet_sha256,'deadline':context.deadline}


def publish_locator(context):
    """One atomic hint, never an authority, queue or source of lease facts."""
    raw=json.dumps({'job_id':context.job_id,'attempt_id':context.attempt_id},
                   sort_keys=True,separators=(',',':')).encode()
    directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    temporary=None
    try:
        for part in LOCATOR.parent.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=child
            info=os.fstat(directory)
            need(info.st_uid in {0,os.getuid()}and (not info.st_mode&0o022 or
                 info.st_uid==0 and bool(info.st_mode&stat.S_ISVTX)))
        info=os.fstat(directory)
        need(info.st_uid==os.getuid()and info.st_gid==os.getgid()and stat.S_IMODE(info.st_mode)==0o750)
        def existing():
            fd=os.open(LOCATOR.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            try:
                info=os.fstat(fd)
                need(stat.S_ISREG(info.st_mode)and info.st_uid==os.getuid()and info.st_gid==os.getgid()
                     and stat.S_IMODE(info.st_mode)==0o440 and info.st_nlink==1 and info.st_size<=1024)
                need(os.read(fd,1025)==raw)
            finally:os.close(fd)
        try:existing();return
        except FileNotFoundError:pass
        temporary='request-'+uuid.uuid4().hex+'.tmp'
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o440,dir_fd=directory)
        try:
            os.fchmod(fd,0o440)
            with os.fdopen(fd,'wb',closefd=False)as stream:stream.write(raw);stream.flush()
            os.fsync(fd)
        finally:os.close(fd)
        try:os.link(temporary,LOCATOR.name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
        except FileExistsError:existing()
        os.unlink(temporary,dir_fd=directory);temporary=None;os.fsync(directory)
    except RuntimeFailure:raise
    except Exception:raise RuntimeFailure('native_vm_activation_unavailable')from None
    finally:
        if temporary is not None:
            try:os.unlink(temporary,dir_fd=directory)
            except FileNotFoundError:pass
        os.close(directory)


def current(owner,run_id,attempt_id):
    """Exact operator read: an active-use check, never historical authority."""
    platform,executor=owner.platform,owner.executor
    try:
        profile=executor.executor.activation_profile()
        with platform.db.sessions.begin()as session:
            platform.db.authority_lock(session)
            job=session.get(Job,run_id,with_for_update=True)
            lease=session.get(WorkerExecutionLease,run_id,with_for_update=True)
            need(job is not None and lease is not None)
            run=job.resolved.get('repository_run')
            need(type(run)is dict and run.get('schema_version')==RUN_SCHEMA)
            attempts=run.get('attempts');need(type(attempts)is list and attempts)
            attempt=attempts[-1];value=attempt.get('activation')
            need(type(value)is dict and set(value)==FIELDS and value['schema_version']==SCHEMA)
            need(job.status=='running'and not job.cancel_requested and lease.phase=='started'
                 and lease.generation==job.generation and lease.lease_until==job.lease_until
                 and attempt['state']in {'awaiting_activation','running'}and attempt['id']==attempt_id
                 and attempt_id==f'{job.id}-{job.attempts}')
            expected={'job_id':job.id,'attempt_id':attempt_id,'workspace_id':run['workspace_id'],
                'repository_id':run['repository_id'],'principal_id':job.caller,'generation':job.generation,
                'fence_sha256':hashlib.sha256(lease.fence.encode()).hexdigest(),
                'packet_sha256':attempt['packet_sha256']}
            need(job.caller==run['principal_id']and all(value[k]==v for k,v in expected.items())
                 and value['fence_sha256']==attempt['fence_sha256'])
            need(all(profile[k]==run[k]for k in ('executor_id','publication_revision','qualification_ref','qualification_sha256')))
            platform.authorize_existing_job(session,job)
            executor.check_authorization(job)
            now=time.time()
            need(all(type(v)in {int,float}and math.isfinite(v)and v>now for v in
                (value['deadline'],lease.execution_deadline,lease.lease_until)))
            need(value['deadline']<=lease.execution_deadline and
                 value['deadline']<=datetime.fromisoformat(run['deadline'].replace('Z','+00:00')).timestamp())
            return {**value,**{k:profile[k]for k in ('executor_id','publication_revision','qualification_ref','vm_id')},
                    'lease_until':lease.lease_until}
    except Exception:
        raise RepositoryRunError('native_activation_unavailable','No current native activation is available',409)from None
