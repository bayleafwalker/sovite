"""Owner candidate export uses exact Git/receipt bytes and existing output release."""
import asyncio
import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from sovite import policy
from sovite.flow import contract_revision
from sovite.output_contracts import approve_output
from sovite.repository_candidate_export import CandidateExportOwner, CandidateExportError
from sovite.repository_runs import RepositoryRunAttemptExecutor
from sovite.service import Platform
from sovite.settings import Settings
from sovite.worker import Worker
from test_repository_runs import BindingResolver, make_repo, make_owner, packet, submit, git, ROOT, CALLER, OTHER, setup
from test_workflows import revision_ref


@pytest.fixture
def prepared(tmp_path):
    bundle, _ = policy.load_bundle(ROOT / 'config/platform.local.yaml')
    bundle['output_contracts'] = [{'id':'candidate-export','source_class':'C3','output_class':'C3',
        'destinations':['private-state'], 'approvers':['cloud-codex'], 'procedure_ref':revision_ref('candidate-review')}]
    config=tmp_path/'bundle.yaml'; config.write_text(yaml.safe_dump(bundle))
    settings=Settings(database_url=f'sqlite:///{tmp_path}/control.db', bundle_path=str(config),
        artifact_path=str(tmp_path/'artifacts'), operator_token='operator-repo-test-token-0000000000',
        caller_tokens={'cloud-codex':CALLER, 'other-user':OTHER}, public_origin='http://testserver',
        secure_cookies=False, require_qualification=False)
    platform=Platform(settings); platform.initialize()
    mirror, base=make_repo(tmp_path)
    resolver=BindingResolver(mirror,base,platform.active()[1])
    async def worker(worktree, *_args):
        (worktree/'src'/'main.txt').write_text('candidate\n')
        git('add','-A',cwd=worktree)
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','candidate',cwd=worktree)
    lifecycle=RepositoryRunAttemptExecutor(platform,resolver,tmp_path/'state',worker)
    owner=make_owner(platform,resolver,executor=lifecycle)
    created=submit(owner,packet(base),idempotency_key='export-test-000000000')
    assert asyncio.run(Worker(platform,repository_run_executor=lifecycle).run_once())
    job=platform.get_job(created['id']); assert job.status=='completed'
    attempt=job.resolved['repository_run']['attempts'][-1]['id']
    exporter=CandidateExportOwner(platform,resolver,lifecycle)
    return platform, exporter, job, attempt, mirror, base


def release(platform, job, content):
    return approve_output(platform, source_job_id=job.id, source_digest='sha256:'+job.artifact_sha256,
        contract_id='candidate-export', expected_contract_revision=contract_revision(platform.active()[0]['output_contracts'][0]),
        expected_generation=platform.active()[1], content=content, evidence_refs=[revision_ref('candidate-review')],
        actor='cloud-codex')['release_id']


def test_export_exact_candidate_bundle_and_release(prepared, tmp_path):
    platform, exporter, job, attempt, mirror, base=prepared
    content=exporter._prepare_for_owner_review(job.id, attempt, principal_id='cloud-codex',workspace_id='workspace-1')
    approved=release(platform,job,content)
    result=exporter.export(job.id,attempt,release_id=approved,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')
    assert result==content
    data=json.loads(result); assert data['base_sha']==base
    assert 'fence' not in result and str(tmp_path) not in result
    receipt_bytes=json.dumps(data['receipt_projection'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    assert hashlib.sha256(receipt_bytes).hexdigest()==data['receipt_projection_sha256']
    assert data['receipt_projection']['tip_sha']==data['tip_sha']
    target=tmp_path/'consumer'; git('clone',str(mirror),str(target))
    bundle=tmp_path/'candidate.bundle'; bundle.write_bytes(base64.b64decode(data['bundle_base64']))
    heads = git('bundle','list-heads',str(bundle),cwd=target).splitlines()
    assert heads == [data['tip_sha'] + ' refs/heads/candidate']
    git('fetch',str(bundle),'refs/heads/candidate:refs/heads/imported',cwd=target)
    assert git('rev-parse','refs/heads/imported',cwd=target)==data['tip_sha']


def test_export_refuses_wrong_recipient_or_destination(prepared):
    platform, exporter, job, attempt, *_=prepared
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id='cloud-codex',workspace_id='workspace-1')
    approved=release(platform,job,content)
    for actor, workspace, destination in [('other-user','workspace-1','private-state'),('cloud-codex','wrong','private-state'),('cloud-codex','workspace-1','operations')]:
        with pytest.raises(CandidateExportError): exporter.export(job.id,attempt,release_id=approved,destination=destination,principal_id=actor,workspace_id=workspace)


def test_current_binding_revocation_refuses(prepared):
    platform, exporter, job, attempt, *_=prepared
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id='cloud-codex',workspace_id='workspace-1')
    approved=release(platform,job,content)
    exporter.resolver.grant_id='replacement-grant'
    with pytest.raises(CandidateExportError): exporter.export(job.id,attempt,release_id=approved,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')


def test_receipt_tampering_refuses(prepared):
    platform, exporter, job, attempt, *_ = prepared
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id='cloud-codex',workspace_id='workspace-1')
    approved=release(platform,job,content)
    receipts=list((exporter.execution.state_root/'receipts').glob('*.json'))
    assert len(receipts)==1
    receipts[0].write_bytes(receipts[0].read_bytes()+b' ')
    with pytest.raises(CandidateExportError): exporter.export(job.id,attempt,release_id=approved,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')


def test_dirty_retained_candidate_refuses(prepared):
    platform, exporter, job, attempt, *_ = prepared
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id='cloud-codex',workspace_id='workspace-1')
    approved=release(platform,job,content)
    worktrees=list((exporter.execution.state_root/'worktrees').glob('*/*'))
    assert len(worktrees)==1
    (worktrees[0]/'src'/'main.txt').write_text('tampered')
    with pytest.raises(CandidateExportError): exporter.export(job.id,attempt,release_id=approved,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')


def test_changed_approved_content_and_unknown_attempt_refuse(prepared):
    platform, exporter, job, attempt, *_ = prepared
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id='cloud-codex',workspace_id='workspace-1')
    wrong=release(platform,job,content+' ')
    with pytest.raises(CandidateExportError): exporter.export(job.id,attempt,release_id=wrong,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')
    with pytest.raises(CandidateExportError): exporter.export(job.id,attempt+'-unknown',release_id=wrong,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')


def test_revocation_during_release_read_refuses(prepared, monkeypatch):
    import sovite.repository_candidate_export as module
    platform, exporter, job, attempt, *_ = prepared
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id='cloud-codex',workspace_id='workspace-1')
    approved=release(platform,job,content)
    original=module.outputs._read
    def revoke(*args):
        result=original(*args)
        exporter.resolver.grant_id='replacement-grant'
        return result
    monkeypatch.setattr(module.outputs,'_read',revoke)
    with pytest.raises(CandidateExportError): exporter.export(job.id,attempt,release_id=approved,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')


def test_replace_ref_cannot_hide_out_of_scope_commit(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    async def worker(worktree, *_):
        (worktree/'src'/'main.txt').write_text('candidate\n')
        git('add','-A',cwd=worktree)
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','candidate',cwd=worktree)
        benign = git('rev-parse','HEAD',cwd=worktree)
        (worktree/'outside.txt').write_text('out of scope\n')
        git('add','outside.txt',cwd=worktree)
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','--amend','--no-edit',cwd=worktree)
        actual = git('rev-parse','HEAD',cwd=worktree)
        git('replace',actual,benign,cwd=worktree)
        git('reset','--hard',actual,cwd=worktree)
    lifecycle=RepositoryRunAttemptExecutor(platform,resolver,tmp_path/'replace-state',worker)
    owner=make_owner(platform,resolver,executor=lifecycle)
    created=submit(owner,packet(base),idempotency_key='replace-attack-000000')
    assert asyncio.run(Worker(platform,repository_run_executor=lifecycle).run_once())
    job=platform.get_job(created['id'])
    assert job.status != 'completed'
    assert job.resolved['repository_run']['attempts'][-1]['state'] != 'completed'


def test_git_boundary_ignores_grafts_and_hostile_environment(tmp_path, monkeypatch):
    from sovite.repository_sessions import _git
    repo=tmp_path/'source'; repo.mkdir()
    git('init',str(repo))
    git('config','user.name','Test',cwd=repo); git('config','user.email','test@example.invalid',cwd=repo)
    git('commit','--allow-empty','-m','base',cwd=repo)
    base=git('rev-parse','HEAD',cwd=repo)
    git('commit','--allow-empty','-m','child',cwd=repo)
    head=git('rev-parse','HEAD',cwd=repo)
    graft=repo/'.git'/'info'/'grafts'; graft.write_text(head+'\n')
    assert git('rev-list','--parents','-1','HEAD',cwd=repo)==head
    monkeypatch.setenv('GIT_GRAFT_FILE',str(graft))
    monkeypatch.setenv('GIT_NO_REPLACE_OBJECTS','0')
    assert _git('-C',str(repo),'rev-list','--parents','-1','HEAD').decode().strip()==head+' '+base


def test_post_capture_replacement_ref_cannot_change_exported_candidate(prepared, tmp_path):
    platform, exporter, job, attempt, mirror, base=prepared
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id='cloud-codex',workspace_id='workspace-1')
    approved=release(platform,job,content)
    data=json.loads(content)
    worktree=next((exporter.execution.state_root/'worktrees').glob('*/*'))
    (worktree/'outside.txt').write_text('replacement-only\n')
    git('add','outside.txt',cwd=worktree)
    git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','replacement',cwd=worktree)
    replacement=git('rev-parse','HEAD',cwd=worktree)
    git('reset','--hard',data['tip_sha'],cwd=worktree)
    git('replace',data['tip_sha'],replacement,cwd=worktree)
    result=exporter.export(job.id,attempt,release_id=approved,destination='private-state',principal_id='cloud-codex',workspace_id='workspace-1')
    assert result == content
    target=tmp_path/'receiving'; git('clone',str(mirror),str(target))
    # The receiving clone must ignore copied local replacement refs too.
    git('replace','-d',data['tip_sha'],cwd=target) if git('replace','-l',cwd=target) else None
    bundle=tmp_path/'retained.bundle'; bundle.write_bytes(base64.b64decode(data['bundle_base64']))
    git('fetch',str(bundle),'refs/heads/candidate:refs/heads/imported',cwd=target)
    assert 'outside.txt' not in git('ls-tree','--name-only','refs/heads/imported',cwd=target).splitlines()


def test_git_boundary_never_invokes_repository_fsmonitor(tmp_path):
    from sovite.repository_sessions import _git
    repo=tmp_path/'source'; repo.mkdir(); git('init',str(repo))
    marker=tmp_path/'fsmonitor-marker'
    hook=tmp_path/'fsmonitor-hook'; hook.write_text('#!/bin/sh\nprintf ran > "'+str(marker)+'"\n')
    hook.chmod(0o700)
    git('config','core.fsmonitor',str(hook),cwd=repo)
    _git('-C',str(repo),'status','--porcelain')
    assert not marker.exists()
