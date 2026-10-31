"""Single-use, owner-observed VM adapter for the protected native runtime.

Cloud owns descriptors, provider observation and guest forced commands. This
module cannot allocate, claim, rebuild or power off provider resources. Without
that installed observer and current signed deployment qualification it never
advertises mutable execution. Explicit fixture construction is unqualified.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib
import os
from pathlib import Path
import re
import time

from .adapters import RuntimeFailure
from .db import Qualification
from .qualification import canonical, verify_qualification
from .repository_candidate_retention import MAX_BYTES
from .repository_native_inference import NativeInferenceBridge, Accounting
from .repository_native_git import NativeGit, EXPANDED_LIMIT, tool_facts, tools_current
from .repository_native_runtime import (ExecutorRelay, NativeAppServer, executable, need, stop_process)
from .repository_runtime import protected_bytes
from .repository_sessions import (_atomic_write, _canonical_json, _canonical_root,
                                   RepositorySessionManager)

SCHEMA = 'sovite-native-vm-driver/v1'
_VERBS = frozenset({'seed-v1','exec-v1','quiesce-v1','export-v1'})
_SHA = re.compile(r'^[0-9a-f]{64}$')
_CHECKS = frozenset({'per-attempt-isolation','credential-containment','exact-binding',
    'durable-checkpoint-and-candidate','predecessor-reconciliation','bounded-usage',
    'native-rpc-routing','native-config-isolation','vm-process-lifecycle','candidate-quiescence-import'})


def _owner_module():
    # Fixed installed distribution only; neither config nor a request can name
    # another package, SDK, observer URL or command implementation.
    return importlib.import_module('vuoro_cloud.native_vm_descriptor')


def _inventory_observer():
    owner=importlib.import_module('vuoro_cloud.native_vm_observer')
    return owner.ProtectedInventoryObserver()


def _source_digest():
    digest = hashlib.sha256()
    for name in ('repository_native_vm.py','repository_native_runtime.py',
                 'repository_native_inference.py','repository_native_git.py','model_gateway_client.py'):
        digest.update(name.encode()+b'\0'+Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def _config(value):
    fields = {'schema_version','executor_id','publication_revision','native_store_path',
              'native_sha256','state_root','descriptor_path','descriptor_owner_uid',
              'routine_credentials_root','qualification_id','accounting'}
    need(type(value) is dict and set(value) == fields)
    need(value['schema_version'] == SCHEMA and _SHA.fullmatch(value['native_sha256']))
    need(type(value['descriptor_owner_uid']) is int and value['descriptor_owner_uid'] in {0,os.getuid()})
    need(re.fullmatch(r'/nix/store/[a-z0-9]{32}-codex-0\.166\.0',value['native_store_path']))
    for field in ('state_root','descriptor_path','routine_credentials_root'):
        need(type(value[field]) is str and Path(value[field]).is_absolute())
    for field in ('executor_id','publication_revision','qualification_id'):
        need(type(value[field]) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value[field]))
    return value


class ForcedSSH:
    """Only fixed forced-command verbs, one observed VM and its routine key."""
    def __init__(self, descriptor, credential_root, state_root, deadline, authorization_check):
        self.descriptor, self.deadline = descriptor, deadline
        self.authorization_check = authorization_check
        observed = descriptor.observed
        need(descriptor.contract == 'cloud.native-guest-stdio.v1'
             and descriptor.ssh_port == 2222 and descriptor.ssh_user == 'root')
        need(re.fullmatch(r'[1-9][0-9]*',str(observed.vm_id)))
        need(observed.routine_credential_ref == f'native-vm:{observed.vm_id}:routine')
        self.key = Path(credential_root)/(str(observed.vm_id)+'.routine')
        # Private material is never placed in native context, guest input or logs.
        protected_bytes(self.key,limit=65536)
        self.known_hosts = Path(state_root)/'known-hosts'
        host = observed.guest_ipv4
        need(re.fullmatch(r'[0-9.]+',host))
        line = f'[{host}]:2222 {observed.host_public_key}\n'.encode()
        _atomic_write(self.known_hosts,line)
        self.ssh = Path('/run/current-system/sw/bin/ssh').resolve(strict=True)
        self.keygen = Path('/run/current-system/sw/bin/ssh-keygen').resolve(strict=True)

    async def verify_key(self):
        result = await asyncio.create_subprocess_exec(str(self.keygen),'-y','-f',str(self.key),
            stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            env={'PATH':'/run/current-system/sw/bin','LANG':'C'},start_new_session=True)
        try:
            output,_ = await asyncio.wait_for(result.communicate(),min(3,max(.001,self.deadline-time.time())))
            need(result.returncode == 0 and len(output) <= 16384)
            public = output.decode('ascii').strip().split()[:2]
            need(public == self.descriptor.observed.routine_public_key.split()[:2]
                 and public != self.descriptor.observed.admin_public_key.split()[:2],
                 'native_routine_credential_refused')
        finally:
            if result.returncode is None:
                await stop_process(result)

    async def process(self, verb, *, cleanup=False):
        need(verb in _VERBS)
        if not cleanup:
            self.authorization_check()
            need(time.time() < self.deadline,'execution_timeout')
        host = self.descriptor.observed.guest_ipv4
        command = [str(self.ssh),'-F','/dev/null','-T','-i',str(self.key),'-p','2222',
            '-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','IdentityAgent=none',
            '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(self.known_hosts),
            '-o','GlobalKnownHostsFile=/dev/null','-o','PasswordAuthentication=no',
            '-o','KbdInteractiveAuthentication=no','-o','ForwardAgent=no',
            '-o','ClearAllForwardings=yes','-o','ControlMaster=no','-o','ControlPath=none',
            '-o','ConnectTimeout=3','root@'+host,verb]
        return await asyncio.create_subprocess_exec(*command,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            env={'PATH':'/run/current-system/sw/bin','LANG':'C'},start_new_session=True,limit=MAX_BYTES+8192)

    async def exchange(self, verb, data=b'', *, limit=MAX_BYTES+8192, cleanup=False):
        process = await self.process(verb,cleanup=cleanup)
        async def consume():
            for offset in range(0,len(data),65536):
                if not cleanup:self.authorization_check()
                process.stdin.write(data[offset:offset+65536])
                await process.stdin.drain()
            process.stdin.close()
            output = bytearray()
            while block := await process.stdout.read(65536):
                need(len(output)+len(block) <= limit,'native_guest_output_too_large')
                output.extend(block)
                if not cleanup:
                    self.authorization_check()
            await process.wait()
            need(process.returncode == 0,'native_guest_operation_uncertain')
            if not cleanup:self.authorization_check()
            return bytes(output)
        try:
            timeout = 8 if cleanup else max(.001,self.deadline-time.time())
            return await asyncio.wait_for(consume(),timeout)
        finally:
            if process.returncode is None:
                await stop_process(process)


class NativeVMDriver:
    supports_repository_repair_context = True

    def __init__(self, config, platform, *, observer=None):
        self.config = _config(config)
        self.platform, self.observer = platform, observer
        self.state_root = _canonical_root(Path(config['state_root']),create=True)
        native = Path(config['native_store_path'])/'libexec/codex/bin/codex'
        self.native = executable(native,config['native_sha256'])
        self.git_facts = tool_facts()

    def execution_profile(self):
        """Only existing signed deployment evidence can supply profile claims."""
        if self.observer is None or self.platform is None:
            return None
        try:
            with self.platform.db.sessions() as session:
                row = session.get(Qualification,self.config['qualification_id'])
                bundle,generation = self.platform.active(session)
                need(row is not None and row.generation == generation
                     and row.profile in {bundle.get('default_profile','local'),'all'}
                     and verify_qualification(row,self.platform.settings))
                need(tools_current(self.git_facts))
                Accounting(self.config['accounting'],self.config['accounting']['model_id'])
                evidence = row.payload['evidence']
                need(evidence.get('evidence_kind') == 'deployment')
                checks = {check['name']:check['status'] for check in evidence['checks']}
                need(all(checks.get(name) == 'passed' for name in _CHECKS))
                facts = evidence['native_vm']
                need(type(facts) is dict and set(facts) == {'driver_sha256','configuration_sha256',
                     'native_store_path','native_sha256','scope','tools','image_sha256','execution_profile_id',
                     'deployment_id','native_closure_sha256','guest_helper_sha256','vm_id'})
                need(all(type(facts[name]) is str and _SHA.fullmatch(facts[name]) for name in
                     ('driver_sha256','configuration_sha256','image_sha256','native_closure_sha256','guest_helper_sha256')))
                need(all(type(facts[name]) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',facts[name])
                         for name in ('execution_profile_id','deployment_id')))
                need(type(facts['vm_id']) is str and re.fullmatch(r'[1-9][0-9]{0,19}',facts['vm_id']))
                observed = self.observer(facts['vm_id'])
                need(observed.vm_id == facts['vm_id'] and all(getattr(observed,name) == facts[name]
                     for name in ('image_sha256','native_store_path','native_sha256','execution_profile_id',
                                  'deployment_id','native_closure_sha256','guest_helper_sha256')))
                config = {key:value for key,value in self.config.items() if key != 'qualification_id'}
                need(facts['driver_sha256'] == _source_digest()
                     and facts['configuration_sha256'] == hashlib.sha256(_canonical_json(config)).hexdigest()
                     and facts['native_store_path'] == self.config['native_store_path']
                     and facts['native_sha256'] == self.config['native_sha256']
                     and facts['scope'] == 'single-use-cloud-vm'
                     and facts['tools'] == self.git_facts
                     and _SHA.fullmatch(facts['image_sha256']))
                return {'schema_version':'sovite-repository-execution-profile/v1','mode':'mutable-native',
                    'executor_id':self.config['executor_id'],'publication_revision':self.config['publication_revision'],
                    'qualification_ref':row.id,'qualification_sha256':hashlib.sha256(canonical(row.payload)).hexdigest(),
                    'qualification_expires_at':datetime.fromtimestamp(min(row.expires_at,self.config['accounting']['valid_until']),timezone.utc).isoformat(),
                    'per_attempt_isolation':True,'credential_containment':True,'exact_binding':True,
                    'durable_checkpoint_and_candidate':True,'predecessor_reconciliation':True,'bounded_usage':True}
        except Exception:
            return None

    async def __call__(self,*_args,**_kwargs):
        raise RuntimeFailure('native_attempt_context_required')

    def for_attempt(self, context):
        need(self.execution_profile() is not None,'native_vm_qualification_unavailable')
        Accounting(self.config['accounting'],context.model['id'])
        owner = _owner_module()
        expected = owner.AttemptIdentity(job_id=context.job_id,attempt_id=context.attempt_id,
            workspace_id=context.workspace_id,repository_id=context.repository_id,generation=context.generation,
            fence_sha256=hashlib.sha256(context.fence.encode()).hexdigest(),deadline=context.deadline)
        descriptor = owner.load_descriptor(self.config['descriptor_path'],expected=expected,
            observe=self.observer,owner_uid=self.config['descriptor_owner_uid'],now=time.time())
        need(descriptor.observed.native_store_path == self.config['native_store_path']
             and descriptor.observed.native_sha256 == self.config['native_sha256'])
        with self.platform.db.sessions() as session:
            row = session.get(Qualification,self.config['qualification_id'])
            facts = row.payload['evidence']['native_vm']
            need(descriptor.observed.vm_id == facts['vm_id']
                 and descriptor.observed.native_closure_sha256 == facts['native_closure_sha256']
                 and descriptor.observed.guest_helper_sha256 == facts['guest_helper_sha256']
                 and descriptor.observed.image_sha256 == facts['image_sha256']
                 and descriptor.observed.execution_profile_id == facts['execution_profile_id']
                 and descriptor.observed.deployment_id == facts['deployment_id'])
        return BoundNativeVM(self,context,descriptor,owner)


class BoundNativeVM:
    def __init__(self, driver, context, descriptor, owner, *, routines=None, bridge_factory=NativeInferenceBridge):
        self.driver, self.context, self.descriptor, self.owner = driver,context,descriptor,owner
        self.routines = routines
        self.bridge_factory = bridge_factory

    async def __call__(self, worktree, goal, deadline, budget, authorization_check, *, repair_context=None):
        context = self.context
        need(deadline == context.deadline,'native_deadline_binding_mismatch')
        need(time.time() < deadline,'execution_timeout')
        original_authorization=authorization_check
        def current_authorization():
            original_authorization()
            current=self.owner.load_descriptor(self.driver.config['descriptor_path'],
                expected=self.descriptor.identity,observe=self.driver.observer,
                owner_uid=self.driver.config['descriptor_owner_uid'],now=time.time())
            need(current == self.descriptor,'native_vm_observation_changed')
        authorization_check=current_authorization
        authorization_check()
        key = hashlib.sha256(_canonical_json({'job':context.job_id,'attempt':context.attempt_id,
                                              'fence_sha256':hashlib.sha256(context.fence.encode()).hexdigest()})).hexdigest()
        root = _canonical_root(self.driver.state_root/key,create=True)
        lock = os.open(root/'attempt.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            intent = root/'intent.json'
            need(not intent.exists(),'native_predecessor_reconciliation_required')
            _atomic_write(intent,_canonical_json({'job_id':context.job_id,'attempt_id':context.attempt_id,
                'phase':'preparing','fence_sha256':hashlib.sha256(context.fence.encode()).hexdigest()}),exclusive=True)
            return await self._run(root,intent,Path(worktree),goal,budget,authorization_check,repair_context)
        finally:
            os.close(lock)

    async def _run(self, root, intent, worktree, goal, budget, authorization_check, repair_context):
        context = self.context
        routines = self.routines or ForcedSSH(self.descriptor,self.driver.config['routine_credentials_root'],
                                               root,context.deadline,authorization_check)
        try:
            base, seed_tip, run = await self._execute_native(
                root,intent,worktree,goal,budget,authorization_check,repair_context,routines)
        finally:
            # This also covers uncertain seed/key/bridge-start outcomes. Cleanup
            # has no candidate effects and remains permitted after revocation.
            raw = await routines.exchange('quiesce-v1',limit=8192,cleanup=True)
            quiescent = self.owner.validate_quiescence(raw,self.descriptor)
        authorization_check()
        export = await routines.exchange('export-v1')
        import io
        header, bundle = self.owner.read_bundle_frame(io.BytesIO(export),exported=True)
        need(header['quiescence'] == quiescent and header['original_base_sha'] == base
             and quiescent['original_base_sha'] == base,'native_export_scope_mismatch')
        await self._import(root,worktree,base,seed_tip,header,bundle,run['allowed_areas'],authorization_check)
        _atomic_write(intent,_canonical_json({'job_id':context.job_id,'attempt_id':context.attempt_id,
            'phase':'imported','tip_sha':header['tip_sha']}))

    async def _execute_native(self,root,intent,worktree,goal,budget,authorization_check,repair_context,routines):
        context = self.context
        git = NativeGit(context.deadline,authorization_check,expected_tools=self.driver.git_facts)
        authorization_check()
        await routines.verify_key()
        bridge = self.bridge_factory(context,budget,authorization_check,self.driver.config['accounting'])
        job = self.driver.platform.get_job(context.job_id)
        run = job.resolved['repository_run']
        base = run['base_sha']
        seed_tip = (await git('-C',str(worktree),'rev-parse','HEAD')).decode().strip()
        need(seed_tip == (repair_context['candidate_tip_sha'] if repair_context else base),
             'native_seed_mismatch')
        # A fresh bare repository avoids changing any shared mirror reference.
        seed_repository = root/'seed.git'
        await git('init','--bare',str(seed_repository))
        await git('--git-dir='+str(seed_repository),'fetch','--no-write-fetch-head',str(worktree),
             seed_tip+':refs/heads/seed')
        seed_path = root/'seed.bundle'
        await git('--git-dir='+str(seed_repository),'bundle','create',str(seed_path),'refs/heads/seed')
        need(seed_path.stat().st_size <= 64*1024*1024,'native_seed_too_large')
        seed = seed_path.read_bytes()
        header = {'schema_version':'cloud.native-vm-seed.v1','base_sha':base,'seed_tip_sha':seed_tip,
                  'bundle_bytes':len(seed),'bundle_sha256':hashlib.sha256(seed).hexdigest()}
        await routines.exchange('seed-v1',_canonical_json(header)+b'\n'+seed,limit=8192)
        _atomic_write(intent,_canonical_json({'job_id':context.job_id,'attempt_id':context.attempt_id,
            'phase':'executing','fence_sha256':hashlib.sha256(context.fence.encode()).hexdigest()}))
        relay = ExecutorRelay(lambda:routines.process('exec-v1'),context.deadline,authorization_check)
        native_home = _canonical_root(root/'native-home',create=True)
        app = None
        operation = None
        try:
            async with bridge.listen(),relay.listen():
                app = NativeAppServer(self.driver.native,native_home,context.model['model'],bridge.origin,
                                      bridge.token,context.deadline,authorization_check)
                await app.start()
                operation = asyncio.create_task(app.run(relay,environment_id='attempt-'+root.name[:32],
                    guest_cwd=self.descriptor.guest_cwd,goal=goal,repair_context=repair_context))
                lost = asyncio.create_task(relay.failure.wait())
                inference_failed = asyncio.create_task(bridge.failure.wait())
                try:
                    done,_ = await asyncio.wait({operation,lost,inference_failed},
                        timeout=max(.001,context.deadline-time.time()),return_when=asyncio.FIRST_COMPLETED)
                    need(operation in done and not relay.failed and not bridge.failed,
                         'external_execution_uncertain')
                    await operation
                finally:
                    for task in (operation,lost,inference_failed):
                        if not task.done():task.cancel()
                    await asyncio.gather(operation,lost,inference_failed,return_exceptions=True)
                    await relay.close()
                    if app is not None:await app.close()
        finally:
            if app is not None:await app.close()
            await relay.close()
        return base, seed_tip, run

    async def _import(self,root,worktree,base,seed_tip,header,bundle,areas,authorization_check):
        git = NativeGit(self.context.deadline,authorization_check,expected_tools=self.driver.git_facts)
        # Quarantine parsing precedes any modification of the protected manager
        # worktree. No candidate configuration, hooks or executable is imported.
        quarantine = root/'quarantine.git'
        await git('init','--bare',str(quarantine))
        await git('--git-dir='+str(quarantine),'fetch','--no-write-fetch-head',str(worktree),
             base+':refs/heads/base',seed_tip+':refs/heads/seed')
        path = root/'candidate.bundle'
        _atomic_write(path,bundle,exclusive=True)
        await git('--git-dir='+str(quarantine),'bundle','verify',str(path))
        heads = (await git('bundle','list-heads',str(path))).decode().strip()
        tip = header['tip_sha']
        need(heads == tip+' refs/heads/candidate','native_export_scope_mismatch')
        await git('--git-dir='+str(quarantine),'fetch','--no-write-fetch-head',str(path),
             'refs/heads/candidate:refs/heads/candidate')
        await git('--git-dir='+str(quarantine),'merge-base','--is-ancestor',seed_tip,tip)
        lines = (await git('--git-dir='+str(quarantine),'rev-list','--parents',base+'..'+tip)).decode().splitlines()
        need(all(len(line.split()) == 2 for line in lines),'native_candidate_history_refused')
        changed = (await git('--git-dir='+str(quarantine),'diff','--no-renames','--name-only','-z',base,tip)).split(b'\0')
        expanded_bytes = 0
        for raw in filter(None,changed):
            name = raw.decode('utf-8','strict')
            need(not any(part in {'..','.git'} for part in Path(name).parts)
                 and not name.startswith('/') and RepositorySessionManager._within_area(name,tuple(areas)),
                 'native_candidate_scope_refused')
            entry = await git('--git-dir='+str(quarantine),'ls-tree','-z',tip,'--',name)
            if entry:
                need(entry.split(b' ',1)[0] in {b'100644',b'100755'},'native_candidate_scope_refused')
                object_id = entry.split(b'\t',1)[0].split()[-1].decode('ascii')
                expanded_bytes += int(await git('--git-dir='+str(quarantine),'cat-file','-s',object_id))
                need(expanded_bytes <= EXPANDED_LIMIT,'native_candidate_expansion_refused')
        authorization_check()
        need((await git('-C',str(worktree),'rev-parse','HEAD')).decode().strip() == seed_tip,'native_seed_changed')
        await git('-C',str(worktree),'fetch','--no-write-fetch-head',str(quarantine),'refs/heads/candidate')
        authorization_check()
        # Materialize with the fresh quarantine config, never the manager's
        # local filters. Seed the index first so unchanged canonical files are
        # not rewritten. Candidate .gitattributes cannot name a configured
        # executable filter in this repository.
        placement = ['--git-dir='+str(quarantine),'--work-tree='+str(worktree),'-c','core.bare=false']
        await git(*placement,'read-tree',seed_tip)
        await git(*placement,'read-tree','--reset','-u',tip)
        authorization_check()
        await git('-C',str(worktree),'read-tree',tip)
        await git('-C',str(worktree),'update-ref','HEAD',tip,seed_tip)


def create_driver(config, *, platform=None):
    # One fixed installed Cloud reader. It reads only protected, fresh public
    # inventory; collector/provider/admin credentials stay in the Cloud lane.
    try:
        observer=_inventory_observer()
    except ImportError:
        observer=None
    return NativeVMDriver(config,platform,observer=observer)
