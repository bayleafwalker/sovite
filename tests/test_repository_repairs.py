"""Actual owner/Git/output-release + released Workspace HTTP/PG repair admission.

The mutable callback is a deterministic test process, not native qualification.
PostgreSQL concurrency uses separate Platform connections and a start barrier.
"""
import asyncio
import copy
import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import create_engine

from test_repository_candidate_export import prepared, release
from test_workflows import revision_ref
from sovite.output_contracts import approve_output
from sovite.flow import contract_revision
from test_repository_candidate_retention import served_workspace
from test_repository_runs import make_owner, git
from sovite.repository_candidate_retention import CandidateRetention, CandidatePublicationService, WorkspaceTarget
from sovite.repository_repairs import RepositoryRepairService, RepairPolicy, preview_id
from sovite.service import Platform, PlatformError
from sovite.worker import Worker
from sovite.db import Job


@pytest.fixture
def candidate_model_destination(request):
    return getattr(request, 'param', True)


@pytest.fixture
def candidate_destinations(served_workspace, candidate_model_destination):
    return ['workspace-'+served_workspace[2]] + (['private-runtime'] if candidate_model_destination else [])


@pytest.fixture
def candidate_additional_contracts():
    return [{'id':'repair-feedback', 'source_class':'C3', 'output_class':'C3',
             'destinations':['private-runtime'], 'approvers':['operator'],
             'procedure_ref':revision_ref('trusted-repair-feedback')}]


def body_for(platform, job, base, content, feedback_release):
    # The owner-issued canonical base and full goal are preserved exactly.
    sha = hashlib.sha256(content.encode()).hexdigest()
    body = {'schema_version':'sovite-repository-repair/v1', 'parent_run_id':job.id,
            'candidate_sha256':sha, 'feedback_release_id':feedback_release, 'max_cost_usd':job.resolved['repository_run']['max_cost_usd']}
    body['preview_id'] = preview_id(job.caller, job.id, sha, feedback_release, body['max_cost_usd'])
    return body


async def configured(prepared, served_workspace, tmp_path):
    from vuoro_client.profile import Profile
    platform, exporter, job, attempt, mirror, base = prepared
    client, loss, ws, *_ = served_workspace
    content = exporter._prepare_for_owner_review(job.id, attempt, principal_id=job.caller, workspace_id='workspace-1')
    approved = release(platform, job, content)
    target = WorkspaceTarget('https://workspace.invalid', ws, 'workspace-'+ws, frozenset({'C3'}))
    c = client('producer'); await c.__aenter__()
    retention = CandidateRetention(tmp_path/'publish', target, c)
    key = await retention.prepare(exporter, job.id, attempt, release_id=approved,
                                   principal_id=job.caller, workspace_id='workspace-1')
    receipt = await retention.retain(exporter, key)
    seed = json.loads(content)
    feedback = {'schema_version':'sovite-repository-repair-feedback/v1', 'parent_run_id':job.id,
                'candidate_export_sha256':receipt['sha256'], 'candidate_tip_sha':seed['tip_sha'],
                'result':'check_failed', 'feedback':'The trusted fixture check requires repaired content.'}
    contract = next(c for c in platform.active()[0]['output_contracts'] if c['id']=='repair-feedback')
    feedback_release = approve_output(platform, source_job_id=job.id, source_digest='sha256:'+job.artifact_sha256,
        contract_id='repair-feedback', expected_contract_revision=contract_revision(contract),
        expected_generation=platform.active()[1], content=json.dumps(feedback),
        evidence_refs=[revision_ref('trusted-check-result')], actor='operator', is_operator=True)['release_id']
    async def repair(worktree, goal, deadline, budget, authorization_check, *, repair_context=None):
        assert goal == job.task
        assert repair_context['parent_run_id'] == job.id
        assert repair_context['candidate_tip_sha'] == git('rev-parse','HEAD',cwd=worktree)
        assert (worktree/'src/main.txt').read_text() == 'candidate\n'
        assert repair_context['feedback'] == feedback['feedback']
        budget.acquire_slot('repair-fixture')
        budget.reserve_cost('repair-fixture-call', 0.5)
        (worktree/'src/main.txt').write_text('repaired\n')
        git('add','-A',cwd=worktree)
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','repair',cwd=worktree)
        budget.settle_cost('repair-fixture-call', 0.25)
        budget.release_slot('repair-fixture')
    repair.supports_repository_repair_context = True
    repair.bound_contexts = []
    def bind(context):
        assert context.workspace_id == 'workspace-1' and context.repository_id == 'repo-1'
        assert context.principal_id == job.caller and context.generation == job.generation
        assert context.fence not in repr(context)
        repair.bound_contexts.append(context)
        return repair
    repair.for_attempt = bind
    exporter.execution.executor = repair
    owner = make_owner(platform, exporter.resolver, executor=exporter.execution)
    reader = CandidatePublicationService(exporter, tmp_path/'publisher-reader', target,
        Profile('repair','https://workspace.invalid','producer'), lambda ref:ref, transport=loss)
    service = RepositoryRepairService(owner, reader, RepairPolicy(2, ('repair-feedback',)))
    return service, body_for(platform, job, base, content, feedback_release), c


def test_actual_retained_seed_same_goal_lost_reply_and_sibling_refusal(prepared, served_workspace, tmp_path):
    async def scenario():
        service, body, client = await configured(prepared, served_workspace, tmp_path)
        try:
            first = await service.submit(body, principal_id='cloud-codex', idempotency_key='repair-response-loss-0001')
            # Caller discards the first reply and reconstructs its exact intent.
            second = await service.submit(copy.deepcopy(body), principal_id='cloud-codex', idempotency_key='repair-response-loss-0001')
            assert second == first
            with pytest.raises(PlatformError, match='current owner authority') as refusal:
                await service.submit(body, principal_id='cloud-codex', idempotency_key='repair-sibling-attempt-0002')
            assert refusal.value.code == 'repository_repair_successor_exists'
            platform = service.owner.platform
            assert await Worker(platform, repository_run_executor=service.owner.executor).run_once()
            child = platform.get_job(first['run_id'])
            assert child.status == 'completed', child.error_code
            assert child.task == prepared[2].task
            assert child.resolved['repository_run']['base_sha'] == prepared[5]
            assert child.resolved['repository_run']['repair']['repair_number'] == 1
            assert child.resolved['repository_run']['usage_usd'] == 0.25
            contexts = service.owner.executor.executor.bound_contexts
            assert len(contexts) == 1 and contexts[0].job_id == child.id
            assert contexts[0].attempt_id == child.id+'-'+str(child.attempts)
            projected = service.owner.get(child.id, principal_id='cloud-codex', workspace_id='workspace-1')
            assert [e for e in projected['evidence'] if e['label']=='repair-predecessor'] == [{
                'label':'repair-predecessor', 'uri':'sovite://repository-runs/'+body['parent_run_id'],
                'sha256':body['candidate_sha256']}]
        finally:
            await client.__aexit__(None, None, None)
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['unknown', 'overspend', 'foreign-preview', 'wrong-feedback-contract', 'increased-cap'])
def test_repair_refuses_unknown_usage_overbudget_or_changed_binding(prepared, served_workspace, tmp_path, fault):
    async def scenario():
        service, body, client = await configured(prepared, served_workspace, tmp_path)
        try:
            platform = service.owner.platform
            if fault in {'unknown','overspend'}:
                with platform.db.sessions.begin() as session:
                    parent = session.get(Job, body['parent_run_id'])
                    resolved = copy.deepcopy(parent.resolved)
                    run = resolved['repository_run']
                    if fault == 'unknown':
                        run['budget_reservations'] = [{'call_id':'unknown-call', 'state':'reserved', 'reserved_usd':0.1, 'actual_usd':None}]
                    else:
                        run['usage_usd'] = 0.5
                    parent.resolved = resolved
            elif fault == 'foreign-preview':
                body['preview_id'] = preview_id('other-user', body['parent_run_id'], body['candidate_sha256'], body['feedback_release_id'], body['max_cost_usd'])
            elif fault == 'wrong-feedback-contract':
                service.policy = RepairPolicy(2, ('different-feedback-procedure',))
            else:
                body['max_cost_usd'] += 0.1
                body['preview_id'] = preview_id('cloud-codex', body['parent_run_id'], body['candidate_sha256'], body['feedback_release_id'], body['max_cost_usd'])
            with pytest.raises(PlatformError):
                await service.submit(body, principal_id='cloud-codex', idempotency_key='repair-refused-intent-0001')
            assert service.owner.platform.get_job(body['parent_run_id']).status == 'completed'
            assert len(service.owner.list(principal_id='cloud-codex', workspace_id='workspace-1')) == 1
        finally:
            await client.__aexit__(None, None, None)
    asyncio.run(scenario())


def test_postgresql_independent_successor_admission_is_serialized(prepared, served_workspace, tmp_path):
    url = os.environ.get('SOVITE_TEST_POSTGRES_URL')
    if not url and os.environ.get('SOVITE_REPAIR_REQUIRE_PG') == '1':
        pytest.fail('SOVITE_TEST_POSTGRES_URL required for repair CI')
    if not url:
        pytest.skip('SOVITE_TEST_POSTGRES_URL required for actual PostgreSQL concurrency')
    if url.startswith(('postgresql://','postgres://')):
        url = 'postgresql+psycopg://' + url.split('://',1)[1]
    from sovite.db import Base
    platform, exporter, job, attempt, mirror, base = prepared
    admin = create_engine(url)
    schema = 'repair_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    postgres = Platform(replace(platform.settings, database_url=url, database_schema=schema))
    postgres.db.migrate()
    # Copy only this disposable fixture's actual owner records to exercise the
    # same history through independently connected PostgreSQL transactions.
    with platform.db.engine.connect() as source, postgres.db.engine.begin() as target:
        for table in reversed(Base.metadata.sorted_tables):
            target.execute(table.delete())
        for table in Base.metadata.sorted_tables:
            rows = [dict(row) for row in source.execute(table.select()).mappings()]
            if rows:
                target.execute(table.insert(), rows)
    exporter.platform = exporter.execution.platform = postgres
    replacement = (postgres, exporter, postgres.get_job(job.id), attempt, mirror, base)
    clients = served_workspace[0]
    async def prepare():
        service, body, client = await configured(replacement, served_workspace, tmp_path)
        target = service.retention.target
        await client.__aexit__(None,None,None)
        return service.owner, body, target
    owner, body, target = asyncio.run(prepare())
    start = threading.Barrier(2)
    def submit(number):
        async def request():
            async with clients('producer') as client:
                retention = CandidateRetention(tmp_path/('reader-'+str(number)), target, client)
                service = RepositoryRepairService(owner, retention, RepairPolicy(2, ('repair-feedback',)))
                start.wait(timeout=10)
                try:
                    return await service.submit(body, principal_id='cloud-codex',
                                                idempotency_key='independent-repair-request-000'+str(number))
                except PlatformError as error:
                    return error.code
        return asyncio.run(request())
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, number) for number in (1,2)]
            results = [future.result(timeout=30) for future in futures]
        assert sum(isinstance(result, dict) for result in results) == 1
        assert results.count('repository_repair_successor_exists') == 1
        assert len(owner.list(principal_id='cloud-codex', workspace_id='workspace-1')) == 2
    finally:
        postgres.db.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_generated_http_contract_rejects_extensions_and_admits_exact_current_intent(prepared, served_workspace, tmp_path):
    import httpx
    from sovite.api import create_app
    from test_repository_runs import CALLER
    async def scenario():
        service, body, client = await configured(prepared, served_workspace, tmp_path)
        app = create_app(service.owner.platform.settings, service.owner.platform, repository_run_owner=service.owner)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
            headers={'Authorization':'Bearer '+CALLER, 'Idempotency-Key':'repair-generated-http-0001'}) as http:
            try:
                response = await http.post('/api/repository-runs/v1/repairs', json=body)
                assert response.status_code == 503
                app.state.repository_repair_service = service
                response = await http.post('/api/repository-runs/v1/repairs', json=body | {'packet':{'goal':'changed'}})
                assert response.status_code == 422
                response = await http.post('/api/repository-runs/v1/repairs', json=body)
                assert response.status_code == 201, response.json()
                assert (await http.post('/api/repository-runs/v1/repairs', json=body)).json() == response.json()
            finally:
                await client.__aexit__(None,None,None)
    asyncio.run(scenario())


@pytest.mark.parametrize('candidate_model_destination', [False], indirect=True)
def test_workspace_read_does_not_authorize_candidate_model_disclosure(prepared, served_workspace, tmp_path):
    async def scenario():
        service, body, client = await configured(prepared, served_workspace, tmp_path)
        try:
            with pytest.raises(PlatformError):
                await service.submit(body, principal_id='cloud-codex', idempotency_key='candidate-storage-only-0001')
            assert len(service.owner.list(principal_id='cloud-codex', workspace_id='workspace-1')) == 1
        finally:
            await client.__aexit__(None,None,None)
    asyncio.run(scenario())


def test_revoked_candidate_release_stops_admitted_child_despite_live_feedback(prepared, served_workspace, tmp_path):
    async def scenario():
        service, body, client = await configured(prepared, served_workspace, tmp_path)
        try:
            child = await service.submit(body, principal_id='cloud-codex', idempotency_key='candidate-revocation-0001')
            platform = service.owner.platform
            # Candidate's approving caller is disabled, while the separate
            # feedback's operator and exact feedback contract stay current.
            platform.settings.caller_tokens.pop('cloud-codex')
            with platform.db.sessions.begin() as session:
                row = session.get(Job, child['run_id'])
                with pytest.raises(PlatformError) as refusal:
                    platform.authorize_existing_job(session, row)
                assert refusal.value.code == 'declassification_denied'
        finally:
            await client.__aexit__(None,None,None)
    asyncio.run(scenario())
