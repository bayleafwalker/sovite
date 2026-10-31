"""Exact interrupted-attempt capture; never starts a native or model process."""
from __future__ import annotations

from dataclasses import asdict
import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace

from .repository_native_git import NativeGit
from .repository_native_runtime import need
from .repository_runtime import protected_bytes
from .repository_sessions import _atomic_write, _canonical_json, _canonical_root, _unique_pairs


def _read(path,limit=65536):
    return json.loads(protected_bytes(path,limit=limit),object_pairs_hook=_unique_pairs)


async def recover_attempt(driver, *, job, packet, worktree, authorization_check):
    from .repository_native_vm import BoundNativeVM, ForcedSSH, _owner_module, _source_digest
    authorization_check()
    key=hashlib.sha256(_canonical_json({'job':packet.job_id,'attempt':packet.attempt_id,
        'fence_sha256':hashlib.sha256(packet.fence.encode()).hexdigest()})).hexdigest()
    root=_canonical_root(driver.state_root/key,create=False)
    lock=os.open(root/'attempt.lock',os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        saved=_read(root/'recovery-context.json')
        fields={'schema_version','job_id','attempt_id','principal_id','packet_sha256','base_sha',
                'seed_tip_sha','allowed_areas','driver_sha256','descriptor'}
        need(type(saved)is dict and set(saved)==fields
            and saved['schema_version']=='sovite-native-recovery-context/v1'
            and saved['job_id']==packet.job_id==job.id and saved['attempt_id']==packet.attempt_id
            and saved['principal_id']==job.caller
            and saved['packet_sha256']==hashlib.sha256(_canonical_json(packet.as_dict())).hexdigest()
            and saved['base_sha']==packet.base_sha
            and saved['allowed_areas']==list(packet.allowed_areas)
            and saved['driver_sha256']==_source_digest(),'native_recovery_context_unavailable')
        payload_path=root/'recovery-payload.json'
        if payload_path.exists():
            # Validated host bytes have independent current-reader authority;
            # no VM/model replay or expired producer lease is needed to record
            # an acknowledgement lost after this immutable write.
            payload=_read(payload_path,limit=8*1024*1024)
            authorization_check()
            return payload
        owner=_owner_module()
        expected=owner.AttemptIdentity(**saved['descriptor']['identity'])
        from .repository_runs import _timestamp
        need(expected.job_id==job.id and expected.attempt_id==packet.attempt_id
             and expected.workspace_id==packet.workspace_id
             and expected.repository_id==packet.repository_id and expected.generation==packet.generation
             and expected.fence_sha256==hashlib.sha256(packet.fence.encode()).hexdigest()
             and expected.deadline<=_timestamp(packet.deadline,'deadline').timestamp()
             and type(saved['seed_tip_sha'])is str and re.fullmatch(r'[0-9a-f]{40}',saved['seed_tip_sha']),
             'native_recovery_context_unavailable')
        def current():
            authorization_check()
            value=owner.load_recovery_descriptor(driver.config['descriptor_path'],expected=expected,
                observe=driver.observer,owner_uid=driver.config['descriptor_owner_uid'],now=time.time())
            need(asdict(value)==saved['descriptor'],'native_vm_observation_changed')
            return value
        descriptor=current()
        deadline=min(time.time()+60,descriptor.recovery_until)
        routines=ForcedSSH(descriptor,driver.config['routine_credentials_root'],root,deadline,current)
        await routines.verify_key()
        raw=await routines.exchange('quiesce-v1',limit=8192,cleanup=True)
        ack=owner.validate_quiescence(raw,descriptor)
        current()
        frame=await routines.exchange('export-v1')
        header,bundle=owner.read_bundle_frame(io.BytesIO(frame),exported=True)
        need(header['quiescence']==ack and ack['original_base_sha']==packet.base_sha
             and header['original_base_sha']==packet.base_sha,'native_export_scope_mismatch')
        git=NativeGit(deadline,current,expected_tools=driver.git_facts)
        # Scratch state is disposable. A crash never turns it into a second
        # capture or selects a newer VM snapshot; the guest returns sealed bytes.
        with tempfile.TemporaryDirectory(prefix='recovery-',dir=root) as temporary:
            scratch=Path(temporary);candidate=scratch/'worktree'
            await git('init',str(candidate))
            await git('-C',str(candidate),'fetch','--no-write-fetch-head',str(worktree),
                packet.base_sha+':refs/heads/base',saved['seed_tip_sha']+':refs/heads/seed')
            await git('-C',str(candidate),'checkout','--detach',saved['seed_tip_sha'])
            bound=BoundNativeVM(driver,SimpleNamespace(deadline=deadline),descriptor,owner)
            await bound._import(scratch,candidate,packet.base_sha,saved['seed_tip_sha'],header,bundle,
                                saved['allowed_areas'],current)
            patch=await git('-C',str(candidate),'diff','--no-ext-diff','--no-textconv','--binary',
                            '--no-renames',packet.base_sha,header['tip_sha'],'--')
            paths=(await git('-C',str(candidate),'diff','--no-renames','--name-only','-z',
                            packet.base_sha,header['tip_sha'],'--')).decode().strip('\0').split('\0')
        projection={'job_id':job.id,'attempt_id':packet.attempt_id,'workspace_id':packet.workspace_id,
            'repository_id':packet.repository_id,'generation':packet.generation,'base_sha':packet.base_sha,
            'tip_sha':header['tip_sha'],'seed_tip_sha':saved['seed_tip_sha'],
            'packet_sha256':saved['packet_sha256'],'patch_sha256':hashlib.sha256(patch).hexdigest(),
            'changed_paths':list(filter(None,paths)),'within_deadline':False,
            'publication_eligible':False,'repair_eligible':False,
            'scope_violations':['interrupted_execution'],'quiescence':ack}
        from .repository_recovery import outcome_for
        payload={'schema_version':'sovite-repository-candidate-export/v1',
            'kind':'repository-recovery-artifact','run_id':job.id,'attempt_id':packet.attempt_id,
            'outcome':outcome_for(job,packet.attempt_id),'publication_eligible':False,'repair_eligible':False,
            'workspace_id':packet.workspace_id,'repository_id':packet.repository_id,
            'base_sha':packet.base_sha,'tip_sha':header['tip_sha'],
            'patch_sha256':projection['patch_sha256'],'receipt_projection':projection,
            'receipt_projection_sha256':hashlib.sha256(_canonical_json(projection)).hexdigest(),
            'bundle_sha256':hashlib.sha256(bundle).hexdigest(),
            'bundle_base64':base64.b64encode(bundle).decode('ascii')}
        need(len(_canonical_json(payload))<=8*1024*1024,'native_guest_output_too_large')
        current()
        _atomic_write(payload_path,_canonical_json(payload),exclusive=True)
        return payload
    finally:
        os.close(lock)
