"""Consumer refusal controls; these fixtures never qualify a real Cloud VM."""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from sovite.adapters import RuntimeFailure
import sovite.repository_native_git as native_git
from sovite.repository_native_vm import BoundNativeVM, NativeVMDriver
from sovite.repository_sessions import _git


@pytest.fixture(autouse=True)
def shared_workstation_uid_fixture(monkeypatch):
    # The workstation has >1,000 existing threads under this shared UID.
    # Production keeps its fixed 64-process bound under a dedicated supervisor
    # UID. This explicit test-only bound allows local Git subprocesses; it is
    # not an installed configuration knob or qualification of process isolation.
    monkeypatch.setattr(native_git,'_PROCESS_LIMIT',4096)
    # Git semantics/refusal tests also run on Ubuntu CI. Only this fixture
    # substitutes the actual installed public system tools for fixed Nix paths;
    # the production parser accepts no tool-path setting and remains Nix-only.
    monkeypatch.setattr(native_git,'_tool',lambda name:Path(shutil.which(name)).resolve(strict=True))
    monkeypatch.setattr(native_git,'_git_exec_path',lambda binary:Path(subprocess.run(
        [str(binary),'--exec-path'],check=True,capture_output=True,text=True).stdout.strip()))


def commit(repository, name, value, *, symlink=False):
    target=repository/name
    target.parent.mkdir(parents=True,exist_ok=True)
    if symlink: target.symlink_to(value)
    else: target.write_text(value)
    _git('-C',str(repository),'add','--',name)
    _git('-C',str(repository),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
         'commit','-m','fixture')
    return _git('-C',str(repository),'rev-parse','HEAD').decode().strip()


@pytest.fixture
def repository(tmp_path):
    path=tmp_path/'worktree';path.mkdir(mode=0o700)
    _git('init',str(path))
    base=commit(path,'src/base.txt','base')
    return path,base


def callback(tmp_path, base):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    context=SimpleNamespace(job_id='job-1',attempt_id='attempt-1',fence='private-fence',
        deadline=time.time()+20,model={'model':'synthetic'})
    platform=SimpleNamespace(get_job=lambda _:SimpleNamespace(resolved={'repository_run':{
        'base_sha':base,'allowed_areas':['src']}}))
    descriptor=SimpleNamespace(identity=SimpleNamespace(),boot_id='fixture-boot')
    driver=SimpleNamespace(state_root=state,platform=platform,observer=lambda _:None,git_facts=native_git.tool_facts(),
        config={'descriptor_path':str(tmp_path/'descriptor'),'descriptor_owner_uid':os.getuid(),'accounting':{}})
    owner=SimpleNamespace(validate_quiescence=lambda raw,_:json.loads(raw),
        load_descriptor=lambda *args,**kwargs:descriptor)
    routines=SimpleNamespace()
    bound=BoundNativeVM(driver,context,descriptor,owner,routines=routines)
    # Legacy transport/import fixtures do not construct a real owner attempt
    # and descriptor. Recovery-context production persistence is covered by
    # the interruption recovery fixture with an actual lifecycle packet.
    bound._persist_recovery=lambda *_args:None
    return bound


def candidate_bundle(tmp_path, worktree, base, *, name='src/result.txt', symlink=False):
    guest=tmp_path/'guest'
    _git('clone','--no-hardlinks',str(worktree),str(guest))
    tip=commit(guest,name,'../../host-secret' if symlink else 'candidate',symlink=symlink)
    _git('-C',str(guest),'update-ref','refs/heads/candidate',tip)
    bundle=tmp_path/'candidate.bundle'
    _git('-C',str(guest),'bundle','create',str(bundle),'refs/heads/candidate','^'+base)
    return tip,bundle.read_bytes()


def test_unknown_guest_seed_outcome_still_requests_quiescence_and_cannot_replay(repository,tmp_path):
    worktree,base=repository
    bound=callback(tmp_path,base)
    calls=[]
    async def key(): pass
    async def exchange(verb,data=b'',**kwargs):
        calls.append((verb,kwargs.get('cleanup',False)))
        if verb=='seed-v1': raise RuntimeFailure('external_execution_uncertain')
        assert verb=='quiesce-v1'
        return json.dumps({'original_base_sha':base}).encode()
    bound.routines.verify_key=key;bound.routines.exchange=exchange
    bound.bridge_factory=lambda *args:SimpleNamespace()
    async def scenario():
        with pytest.raises(RuntimeFailure,match='external_execution_uncertain'):
            await bound(worktree,'unchanged goal',bound.context.deadline,None,lambda:True)
        assert calls==[('seed-v1',False),('quiesce-v1',True)]
        with pytest.raises(RuntimeFailure,match='native_predecessor_reconciliation_required'):
            await bound(worktree,'unchanged goal',bound.context.deadline,None,lambda:True)
        assert len(calls)==2
    asyncio.run(scenario())


def test_revocation_after_quiescence_withholds_export(repository,tmp_path):
    worktree,base=repository
    bound=callback(tmp_path,base);current=True;calls=[]
    async def executed(*args): return base,base,{'allowed_areas':['src']}
    async def exchange(verb,**kwargs):
        nonlocal current
        calls.append(verb);assert verb=='quiesce-v1' and kwargs['cleanup']
        current=False
        return json.dumps({'original_base_sha':base}).encode()
    def authorize():
        if not current:raise RuntimeFailure('authorization_revoked')
    bound._execute_native=executed;bound.routines.exchange=exchange
    async def scenario():
        with pytest.raises(RuntimeFailure,match='authorization_revoked'):
            await bound(worktree,'goal',bound.context.deadline,None,authorize)
    asyncio.run(scenario())
    assert calls==['quiesce-v1']
    assert _git('-C',str(worktree),'rev-parse','HEAD').decode().strip()==base


@pytest.mark.parametrize('failure',['wrong-ack','wrong-base'])
def test_export_requires_exact_previously_checked_owner_quiescence(repository,tmp_path,failure):
    worktree,base=repository;bound=callback(tmp_path,base);calls=[]
    ack={'original_base_sha':base,'boot_id':'known-boot'}
    async def executed(*args): return base,base,{'allowed_areas':['src']}
    async def exchange(verb,**kwargs):
        calls.append(verb)
        return json.dumps(ack).encode() if verb=='quiesce-v1' else b'fixture'
    header={'quiescence':dict(ack),'original_base_sha':base}
    if failure=='wrong-ack':header['quiescence']['boot_id']='other-boot'
    else:header['original_base_sha']='0'*40
    bound.owner.read_bundle_frame=lambda *args,**kwargs:(header,b'')
    bound._execute_native=executed;bound.routines.exchange=exchange
    async def scenario():
        with pytest.raises(RuntimeFailure,match='native_export_scope_mismatch'):
            await bound(worktree,'goal',bound.context.deadline,None,lambda:True)
    asyncio.run(scenario());assert calls==['quiesce-v1','export-v1']
    assert _git('-C',str(worktree),'rev-parse','HEAD').decode().strip()==base


def test_owner_quarantines_then_imports_exact_descendant_bundle(repository,tmp_path):
    worktree,base=repository;bound=callback(tmp_path,base)
    tip,bundle=candidate_bundle(tmp_path,worktree,base)
    root=bound.driver.state_root
    asyncio.run(bound._import(root,worktree,base,base,{'tip_sha':tip},bundle,['src'],lambda:True))
    assert _git('-C',str(worktree),'rev-parse','HEAD').decode().strip()==tip
    assert (worktree/'src/result.txt').read_text()=='candidate'


@pytest.mark.parametrize('name,symlink',[('outside.txt',False),('src/link',True)])
def test_untrusted_candidate_cannot_import_outside_area_or_symlink(repository,tmp_path,name,symlink):
    worktree,base=repository;bound=callback(tmp_path,base)
    tip,bundle=candidate_bundle(tmp_path,worktree,base,name=name,symlink=symlink)
    with pytest.raises(RuntimeFailure,match='native_candidate_scope_refused'):
        asyncio.run(bound._import(bound.driver.state_root,worktree,base,base,
                                  {'tip_sha':tip},bundle,['src'],lambda:True))
    assert _git('-C',str(worktree),'rev-parse','HEAD').decode().strip()==base
    assert not (worktree/name).exists()


def test_revocation_before_import_leaves_protected_worktree_unchanged(repository,tmp_path):
    worktree,base=repository;bound=callback(tmp_path,base)
    tip,bundle=candidate_bundle(tmp_path,worktree,base)
    def revoked():raise RuntimeFailure('authorization_revoked')
    with pytest.raises(RuntimeFailure,match='authorization_revoked'):
        asyncio.run(bound._import(bound.driver.state_root,worktree,base,base,
                                  {'tip_sha':tip},bundle,['src'],revoked))
    assert _git('-C',str(worktree),'rev-parse','HEAD').decode().strip()==base


def test_construction_and_configuration_never_supply_a_qualified_profile():
    driver=object.__new__(NativeVMDriver)
    driver.observer=None;driver.platform=SimpleNamespace()
    driver.config={'qualification_id':'invented','per_attempt_isolation':True}
    assert driver.execution_profile() is None
    with pytest.raises(RuntimeFailure,match='native_vm_qualification_unavailable'):
        driver.for_attempt(SimpleNamespace())


def test_narrower_deadline_refuses_before_any_vm_operation(repository,tmp_path):
    worktree,base=repository;bound=callback(tmp_path,base)
    async def scenario():
        with pytest.raises(RuntimeFailure,match='native_deadline_binding_mismatch'):
            await bound(worktree,'goal',bound.context.deadline-1,None,lambda:True)
    asyncio.run(scenario())
    assert not list(bound.driver.state_root.iterdir())


def test_compressed_bundle_expansion_refuses_before_materializing(repository,tmp_path):
    worktree,base=repository;bound=callback(tmp_path,base)
    tip,bundle=candidate_bundle(tmp_path,worktree,base)
    guest=tmp_path/'guest'
    tip=commit(guest,'src/expanded.txt','A'*(8*1024*1024+1))
    _git('-C',str(guest),'update-ref','refs/heads/candidate',tip)
    path=tmp_path/'large.bundle'
    _git('-C',str(guest),'bundle','create',str(path),'refs/heads/candidate','^'+base)
    compressed=path.read_bytes();assert len(compressed)<65536
    with pytest.raises(RuntimeFailure,match='native_candidate_expansion_refused'):
        asyncio.run(bound._import(bound.driver.state_root,worktree,base,base,{'tip_sha':tip},
                                  compressed,['src'],lambda:True))
    assert not (worktree/'src/expanded.txt').exists()
    assert _git('-C',str(worktree),'rev-parse','HEAD').decode().strip()==base


def test_malformed_bundle_is_not_imported(repository,tmp_path):
    worktree,base=repository;bound=callback(tmp_path,base)
    with pytest.raises(RuntimeFailure,match='native_git_refused'):
        asyncio.run(bound._import(bound.driver.state_root,worktree,base,base,{'tip_sha':'a'*40},
                                  b'not a Git bundle',['src'],lambda:True))
    assert _git('-C',str(worktree),'rev-parse','HEAD').decode().strip()==base


def test_candidate_attributes_cannot_invoke_host_filter_and_no_environment_inheritance(repository,tmp_path,monkeypatch):
    worktree,base=repository;bound=callback(tmp_path,base)
    marker=tmp_path/'host-filter-executed'
    _git('-C',str(worktree),'config','filter.attack.smudge',
         shutil.which('touch')+' '+str(marker)+'; '+shutil.which('cat'))
    monkeypatch.setenv('SOVITE_NATIVE_TEST_HOST_SECRET','host-only-sentinel')
    monkeypatch.setenv('GIT_CONFIG_COUNT','1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0','core.hooksPath')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0','/untrusted/hooks')
    tip,_=candidate_bundle(tmp_path,worktree,base)
    guest=tmp_path/'guest';tip=commit(guest,'src/.gitattributes','result.txt filter=attack\n')
    _git('-C',str(guest),'update-ref','refs/heads/candidate',tip)
    path=tmp_path/'filtered.bundle'
    _git('-C',str(guest),'bundle','create',str(path),'refs/heads/candidate','^'+base)
    original=asyncio.create_subprocess_exec;seen=[]
    async def spawn(*args,**kwargs):
        seen.append(kwargs['env'])
        assert kwargs['start_new_session'] and 'preexec_fn' not in kwargs
        assert Path(args[0]).name=='prlimit' and '--as=536870912:536870912' in args
        return await original(*args,**kwargs)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    asyncio.run(bound._import(bound.driver.state_root,worktree,base,base,{'tip_sha':tip},
                              path.read_bytes(),['src'],lambda:True))
    assert seen and all('SOVITE_NATIVE_TEST_HOST_SECRET' not in env and 'GIT_CONFIG_COUNT' not in env for env in seen)
    assert not marker.exists()
    assert (worktree/'src/result.txt').read_text()=='candidate'
    assert _git('-C',str(worktree),'status','--porcelain')==b''
    # Positive unsafe-checkout control proves this filter can execute here;
    # fresh quarantine materialization above is what prevented the effect.
    _git('-C',str(worktree),'reset','--hard',base)
    _git('-C',str(worktree),'reset','--hard',tip)
    assert marker.exists()


def test_git_whole_deadline_and_live_revocation_stop_a_running_parser():
    from sovite.repository_native_git import NativeGit
    async def scenario(revoke):
        current=True
        def authorize():
            if not current:raise RuntimeFailure('authorization_revoked')
        git=NativeGit(time.time()+(.4 if not revoke else 3),authorize)
        operation=asyncio.create_task(git('-c','alias.wait=!while :; do :; done','wait'))
        if revoke:
            await asyncio.sleep(.1);current=False
        with pytest.raises(RuntimeFailure,match='authorization_revoked' if revoke else 'execution_timeout'):
            await asyncio.wait_for(operation,2)
    asyncio.run(scenario(False));asyncio.run(scenario(True))


def test_existing_qualification_authoring_signs_native_facts_and_tampering_revokes(tmp_path):
    import copy
    import hashlib
    from test_repository_runs import setup as fixture_setup
    from sovite.db import Qualification
    from sovite.qualification import initialize_keys,issue_qualification,verify_qualification,runtime_settings_fingerprint
    from sovite.repository_native_vm import _CHECKS,_source_digest
    from sovite.repository_sessions import _canonical_json
    settings,platform=fixture_setup.__wrapped__(tmp_path)
    initialize_keys(settings)
    generation=platform.active()[1]
    driver=object.__new__(NativeVMDriver)
    # A synthetic observer is sufficient only to exercise source validation;
    # no VM is constructed or claimed by this qualification-format fixture.
    driver.observer=lambda _:None;driver.platform=platform;driver.git_facts=native_git.tool_facts()
    driver.config={'qualification_id':'pending','executor_id':'native-fixture','publication_revision':'fixture',
                   'native_store_path':'/nix/store/'+'a'*32+'-codex-0.166.0','native_sha256':'a'*64,
                   'accounting':{'model_id':'fixture','valid_until':time.time()+600,'maximum_request_bytes':1000,
                       'maximum_input_tokens':10000,'maximum_output_tokens':100,'reservation_usd':1,
                       'input_usd_per_million':1,'output_usd_per_million':2}}
    bound={key:value for key,value in driver.config.items() if key!='qualification_id'}
    facts={'driver_sha256':_source_digest(),'configuration_sha256':hashlib.sha256(_canonical_json(bound)).hexdigest(),
           'native_store_path':driver.config['native_store_path'],'native_sha256':'a'*64,
           'scope':'single-use-cloud-vm','image_sha256':'b'*64,'tools':driver.git_facts,
           'execution_profile_id':'fixture-profile','deployment_id':'fixture-deployment',
           'native_closure_sha256':'d'*64,'guest_helper_sha256':'e'*64,'vm_id':'123456789'}
    driver.observer=lambda _:SimpleNamespace(**facts)
    evidence={'generation':generation,'profile':'local','evidence_kind':'deployment',
        'runtime_binding':runtime_settings_fingerprint(settings),
        'scope':'Synthetic signature-format fixture only; no deployed VM qualification.',
        'checks':[{'name':name,'status':'passed'} for name in sorted(_CHECKS)],'native_vm':facts}
    row=issue_qualification(platform.db,settings,generation,'local',evidence)
    driver.config['qualification_id']=row.id
    assert verify_qualification(row,settings)
    assert driver.execution_profile()['qualification_ref']==row.id
    current_observer=driver.observer
    driver.observer=lambda _:SimpleNamespace(**{**facts,'native_closure_sha256':'f'*64})
    assert driver.execution_profile() is None
    driver.observer=current_observer
    with platform.db.sessions.begin() as session:
        row=session.get(Qualification,row.id)
        modified=copy.deepcopy(row.payload);modified['evidence']['native_vm']['native_sha256']='c'*64
        row.payload=modified
    assert driver.execution_profile() is None


@pytest.mark.parametrize('missing',[False,True])
def test_fixed_factory_without_installed_observer_stays_unavailable(tmp_path,monkeypatch,missing):
    import sovite.repository_native_vm as module
    def absent():raise ImportError('fixed owner package absent')
    monkeypatch.setattr(module,'_inventory_observer',absent if missing else lambda:None)
    # Explicit unit substitute avoids requiring the optional native closure;
    # it grants no observer, qualification, or execution capability.
    monkeypatch.setattr(module,'executable',lambda path,digest:Path('/not-installed-native'))
    config={'schema_version':module.SCHEMA,'executor_id':'fixture','publication_revision':'fixture',
        'native_store_path':'/nix/store/'+'a'*32+'-codex-0.166.0','native_sha256':'a'*64,
        'state_root':str(tmp_path/'state'),'descriptor_path':str(tmp_path/'descriptor.json'),
        'descriptor_owner_uid':os.getuid(),'routine_credentials_root':str(tmp_path/'credentials'),
        'qualification_id':'absent','accounting':{}}
    platform=SimpleNamespace()
    driver=module.create_driver(config,platform=platform)
    assert driver.platform is platform and driver.execution_profile() is None
    with pytest.raises(RuntimeFailure,match='native_vm_qualification_unavailable'):
        driver.for_attempt(SimpleNamespace())
    with pytest.raises(RuntimeFailure):module.create_driver({**config,'per_attempt_isolation':True},platform=platform)


def test_exact_observation_changes_refuse_before_effects_even_with_same_qualification(repository,tmp_path):
    worktree,base=repository;bound=callback(tmp_path,base);checks=[]
    async def executed(root,intent,tree,goal,budget,current,*args):
        current();checks.append('original-boot')
        # Actual installed owner helper re-observes every time. This explicit
        # fixture changes only the observation, leaving caller/grant allowed.
        bound.owner.load_descriptor=lambda *args,**kwargs:SimpleNamespace(
            identity=bound.descriptor.identity,boot_id='replacement-boot')
        current()
        raise AssertionError('A changed VM must not receive another operation')
    async def exchange(verb,**kwargs):
        checks.append(verb);assert verb=='quiesce-v1' and kwargs['cleanup']
        return json.dumps({'original_base_sha':base}).encode()
    bound._execute_native=executed;bound.routines.exchange=exchange
    async def scenario():
        with pytest.raises(RuntimeFailure,match='native_vm_observation_changed'):
            await bound(worktree,'goal',bound.context.deadline,None,lambda:True)
    asyncio.run(scenario())
    assert checks==['original-boot','quiesce-v1']


def test_parser_refuses_a_different_qualified_tool_digest():
    from sovite.repository_native_git import NativeGit
    facts=native_git.tool_facts();facts['git']['sha256']='0'*64
    with pytest.raises(RuntimeFailure,match='native_git_qualification_changed'):
        NativeGit(time.time()+5,lambda:True,expected_tools=facts)


def test_model_preflight_failure_cleans_up_without_seeding_guest(repository,tmp_path):
    worktree,base=repository;bound=callback(tmp_path,base);calls=[]
    async def key():pass
    def unavailable(*args):raise RuntimeFailure('native_model_accounting_unavailable')
    async def exchange(verb,**kwargs):
        calls.append(verb);assert verb=='quiesce-v1' and kwargs['cleanup']
        return json.dumps({'original_base_sha':base}).encode()
    bound.bridge_factory=unavailable;bound.routines.verify_key=key;bound.routines.exchange=exchange
    async def scenario():
        with pytest.raises(RuntimeFailure,match='native_model_accounting_unavailable'):
            await bound(worktree,'goal',bound.context.deadline,None,lambda:True)
    asyncio.run(scenario());assert calls==['quiesce-v1']
