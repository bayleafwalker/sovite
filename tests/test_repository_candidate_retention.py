"""Real Sovite Git/export -> Vuoro HTTP shell -> PostgreSQL/object store.

Set SOVITE_WORKSPACE_TEST_DSN to a disposable PostgreSQL DSN. The pinned owner
extra supplies the actual adapter. Response loss is injected after the service
has handled a request; domain transitions are never mocked.
"""
import asyncio
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

import httpx
import pytest
import psycopg

from test_repository_candidate_export import prepared, release
from sovite.repository_candidate_retention import CandidateRetention, CandidateRetentionError, WorkspaceTarget


@pytest.fixture
def served_workspace(tmp_path):
    dsn = os.environ.get('SOVITE_WORKSPACE_TEST_DSN')
    if not dsn and os.environ.get('SOVITE_WORKSPACE_REQUIRE_PG') == '1':
        pytest.fail('SOVITE_WORKSPACE_TEST_DSN is required for release/CI qualification')
    if not dsn:
        pytest.skip('SOVITE_WORKSPACE_TEST_DSN requires disposable PostgreSQL')
    from vuoro_workspace import migrate_schema, FsObjectStore
    from vuoro_workspace.vuoro_adapter import KnowledgeApplication, register_knowledge_catalog
    from vuoro_service.app import create_app, ServiceSettings
    from vuoro_service.identity import Identity, StaticBearerIdentityResolver
    from vuoro_service.catalog import CatalogRegistry
    schema = 'retention_' + uuid.uuid4().hex[:12]
    role = schema + '_rt'
    conn = psycopg.connect(dsn, autocommit=True)
    migrate_schema(conn, schema)
    conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
    conn.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"')
    def connect():
        c = psycopg.connect(dsn, autocommit=True)
        c.execute(f'SET ROLE "{role}"')
        return c
    app_domain = KnowledgeApplication(connect, FsObjectStore(tmp_path/'objects'), schema=schema, runtime_role=role)
    registry = CatalogRegistry(); register_knowledge_catalog(registry, app_domain)
    every = frozenset({'knowledge:read', 'knowledge:contribute', 'knowledge:review', 'knowledge:admin', 'knowledge:human'})
    identities = {name: Identity(name, 'retention', every, principal_id='test:'+name+':0')
                  for name in ('admin', 'producer', 'successor', 'reader', 'outsider')}
    app = create_app(settings=ServiceSettings(environment_name='retention', compatibility_state='compatible'),
        registry=registry, identity_resolver=StaticBearerIdentityResolver(identities))
    from vuoro_client import AsyncVuoroClient
    from vuoro_client.profile import Profile
    class Loss(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=app); self.operation = None; self.calls = []
            self.pause = None; self.entered = None; self.resume = None
        async def handle_async_request(self, request):
            operation = json.loads(request.content).get('operation') if request.method == 'POST' else None
            if operation: self.calls.append(operation)
            response = await self.inner.handle_async_request(request)
            if operation and operation == self.pause and response.status_code < 300:
                self.entered.set()
                await self.resume.wait()
            if operation and operation == self.operation and response.status_code < 300:
                self.operation = None
                raise httpx.ReadError('synthetic response loss')
            return response
    transport = Loss()
    def client(name):
        return AsyncVuoroClient(Profile('test', 'https://workspace.invalid', name), lambda ref: ref, transport=transport)
    async def seed():
        async with client('admin') as c:
            ws = (await c.invoke('knowledge.workspace.create-v1', {'name':'Candidates'}, idempotency_key='workspace-create-001'))['workspace_id']
            for name, rank in [('producer','contributor'),('successor','contributor'),('reader','reader')]:
                await c.invoke('knowledge.member.set-v1', {'workspace_id':ws, 'principal_id':'test:'+name+':0', 'role':rank}, idempotency_key='member-set-'+name)
        return ws
    ws = asyncio.run(seed())
    yield client, transport, ws, conn, schema
    app_domain.close()
    conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    conn.execute(f'DROP ROLE "{role}"'); conn.close()


@pytest.fixture
def candidate_destinations(served_workspace):
    return ['workspace-' + served_workspace[2]]


@pytest.mark.parametrize('lost', ['knowledge.artifact.begin-v1', 'knowledge.artifact.upload-v1', 'knowledge.artifact.finalize-v1'])
def test_lost_response_fresh_publisher_and_post_expiry_reader(prepared, served_workspace, tmp_path, lost):
    platform, exporter, job, attempt, mirror, base = prepared
    client, loss, ws, conn, schema = served_workspace
    # Fixture includes the exact per-test destination at installation time.
    assert 'workspace-'+ws in platform.active()[0]['output_contracts'][0]['destinations']
    content = exporter._prepare_for_owner_review(job.id, attempt, principal_id='cloud-codex', workspace_id='workspace-1')
    approved = release(platform, job, content)
    target = WorkspaceTarget('https://workspace.invalid', ws, 'workspace-'+ws, frozenset({'C3'}))
    state = tmp_path/'protected-publications'
    async def scenario():
        async with client('producer') as c:
            retention = CandidateRetention(state, target, c)
            key = await retention.prepare(exporter, job.id, attempt, release_id=approved, principal_id='cloud-codex', workspace_id='workspace-1')
            loss.operation = lost
            with pytest.raises(CandidateRetentionError): await retention.retain(exporter, key)
        if lost == 'knowledge.artifact.begin-v1':
            async with client('successor') as c:
                with pytest.raises(CandidateRetentionError): await CandidateRetention(state, target, c).publish(key, exporter)
        async with client('producer') as c:
            receipt = await CandidateRetention(state, target, c).retain(exporter, key)
            assert await CandidateRetention(state, target, c).retain(exporter, key) == receipt
        assert conn.execute(f'SELECT count(*) FROM "{schema}".artifact_version').fetchone()[0] == 1
        # Destroy disposable candidate state and revoke execution. A fresh
        # contributor still reads ready exact bytes using Workspace authority.
        exporter.resolver.grant_id = 'expired-replaced-grant'
        shutil.rmtree(exporter.execution.state_root)
        async with client('successor') as c:
            raw = await CandidateRetention(tmp_path/'successor-state', target, c).recover(receipt)
        assert raw == content.encode()
        assert hashlib.sha256(raw).hexdigest() == receipt['sha256']
        for name in ('reader', 'outsider'):
            async with client(name) as c:
                with pytest.raises(CandidateRetentionError): await CandidateRetention(tmp_path/(name+'-state'), target, c).recover(receipt)
        async with client('admin') as c:
            await c.invoke('knowledge.member.set-v1', {'workspace_id':ws, 'principal_id':'test:successor:0', 'role':None}, idempotency_key='revoke-successor-001')
        async with client('successor') as c:
            with pytest.raises(CandidateRetentionError): await CandidateRetention(tmp_path/'successor-state', target, c).recover(receipt)
        return receipt
    receipt = asyncio.run(scenario())
    evidence = platform.get_job(job.id).resolved['repository_run']['evidence']
    assert len([e for e in evidence if e['label']=='retained-candidate']) == 1
    if lost == 'knowledge.artifact.finalize-v1':
        calls = loss.calls
        assert calls.count('knowledge.artifact.upload-v1') == 1


def test_disclosure_refusal_precedes_network_and_retained_corruption_is_detected(prepared, served_workspace, tmp_path):
    platform, exporter, job, attempt, *_ = prepared
    client, loss, ws, conn, schema = served_workspace
    content = exporter._prepare_for_owner_review(job.id, attempt, principal_id='cloud-codex', workspace_id='workspace-1')
    approved = release(platform, job, content)
    allowed = WorkspaceTarget('https://workspace.invalid', ws, 'workspace-'+ws, frozenset({'C3'}))
    denied = WorkspaceTarget('https://workspace.invalid', ws, 'workspace-'+ws, frozenset({'C1'}))
    async def scenario():
        async with client('producer') as c:
            retention = CandidateRetention(tmp_path/'publication', allowed, c)
            key = await retention.prepare(exporter, job.id, attempt, release_id=approved, principal_id='cloud-codex', workspace_id='workspace-1')
            # Intent already exists but current storage class policy changed.
            count = len(loss.calls)
            with pytest.raises(CandidateRetentionError):
                await CandidateRetention(tmp_path/'publication', denied, c).publish(key, exporter)
            assert len(loss.calls) == count
            receipt = await retention.retain(exporter, key)
            count = len(loss.calls)
            with pytest.raises(CandidateRetentionError):
                await CandidateRetention(tmp_path/'reader', denied, c).recover(receipt)
            assert len(loss.calls) == count
            # The real object store returns changed bytes; the owner's ready
            # read must detect the mismatch, even if the receipt is unchanged.
            files = [p for p in (tmp_path/'objects').rglob('*') if p.is_file()]
            candidate = next(p for p in files if p.read_bytes() == content.encode())
            candidate.write_bytes(content.encode() + b' ')
            with pytest.raises(CandidateRetentionError): await retention.recover(receipt)
    asyncio.run(scenario())


def test_opt_in_actual_repository_http_route_and_identity_scoping(prepared, served_workspace, tmp_path):
    from sovite.api import create_app
    from sovite.repository_candidate_retention import CandidatePublicationService
    from vuoro_client.profile import Profile
    from test_repository_runs import make_owner, CALLER, OTHER
    platform, exporter, job, attempt, *_ = prepared
    client, loss, ws, conn, schema = served_workspace
    content = exporter._prepare_for_owner_review(job.id, attempt, principal_id='cloud-codex', workspace_id='workspace-1')
    approved = release(platform, job, content)
    target = WorkspaceTarget('https://workspace.invalid', ws, 'workspace-'+ws, frozenset({'C3'}))
    service = CandidatePublicationService(exporter, tmp_path/'publisher', target,
        Profile('retention','https://workspace.invalid','producer'), lambda ref:ref, transport=loss)
    owner = make_owner(platform, exporter.resolver, executor=exporter.execution)
    disabled = create_app(settings=platform.settings, platform=platform, repository_run_owner=owner)
    app = create_app(settings=platform.settings, platform=platform, repository_run_owner=owner, candidate_publication_service=service)
    path = '/api/repository-runs/v1/runs/'+job.id+'/candidate-retention'
    body = {'workspace_id':'workspace-1', 'attempt_id':attempt, 'release_id':approved}
    async def scenario():
        async with httpx.AsyncClient(base_url='https://sovite.invalid', transport=httpx.ASGITransport(app=disabled)) as c:
            assert (await c.post(path, json=body, headers={'Authorization':'Bearer '+CALLER})).status_code == 503
        async with httpx.AsyncClient(base_url='https://sovite.invalid', transport=httpx.ASGITransport(app=app)) as c:
            assert (await c.post(path, json=body, headers={'Authorization':'Bearer '+OTHER})).status_code == 404
            result = await c.post(path, json=body, headers={'Authorization':'Bearer '+CALLER})
            assert result.status_code == 200, result.text
            receipt = result.json()['receipt']
            exporter.resolver.grant_id = 'expired-owner-grant'
            shutil.rmtree(exporter.execution.state_root)
            replay = await c.post(path, json=body, headers={'Authorization':'Bearer '+CALLER})
            assert replay.status_code == 200 and replay.json()['receipt'] == receipt
    asyncio.run(scenario())


def test_new_upload_rechecks_output_authority_but_lost_finalize_can_reconcile(prepared, served_workspace, tmp_path):
    platform, exporter, job, attempt, *_ = prepared
    client, loss, ws, conn, schema = served_workspace
    content = exporter._prepare_for_owner_review(job.id, attempt, principal_id='cloud-codex', workspace_id='workspace-1')
    approved = release(platform, job, content)
    target = WorkspaceTarget('https://workspace.invalid', ws, 'workspace-'+ws, frozenset({'C3'}))
    async def scenario():
        async with client('producer') as c:
            retention = CandidateRetention(tmp_path/'publication', target, c)
            key = await retention.prepare(exporter, job.id, attempt, release_id=approved, principal_id='cloud-codex', workspace_id='workspace-1')
            exporter.resolver.grant_id = 'revoked'
            before = list(loss.calls)
            with pytest.raises(CandidateRetentionError): await retention.publish(key, exporter)
            assert 'knowledge.artifact.begin-v1' not in loss.calls[len(before):]
            exporter.resolver.grant_id = 'incident-forensics'
            loss.operation = 'knowledge.artifact.finalize-v1'
            with pytest.raises(CandidateRetentionError): await retention.publish(key, exporter)
            exporter.resolver.grant_id = 'revoked'
        # No new upload is needed: another currently authorized Workspace
        # contributor reconciles the already stored exact version.
        before = loss.calls.count('knowledge.artifact.upload-v1')
        async with client('successor') as c:
            receipt = await CandidateRetention(tmp_path/'publication', target, c).retain(exporter, key)
            assert await CandidateRetention(tmp_path/'reader', target, c).recover(receipt) == content.encode()
        assert loss.calls.count('knowledge.artifact.upload-v1') == before
    asyncio.run(scenario())


def test_concurrent_http_replay_does_not_duplicate_version_or_evidence(prepared, served_workspace, tmp_path):
    from sovite.api import create_app
    from sovite.repository_candidate_retention import CandidatePublicationService
    from vuoro_client.profile import Profile
    from test_repository_runs import make_owner, CALLER
    platform, exporter, job, attempt, *_ = prepared
    client, loss, ws, conn, schema = served_workspace
    content = exporter._prepare_for_owner_review(job.id, attempt, principal_id='cloud-codex', workspace_id='workspace-1')
    approved = release(platform, job, content)
    service = CandidatePublicationService(exporter, tmp_path/'publisher',
        WorkspaceTarget('https://workspace.invalid', ws, 'workspace-'+ws, frozenset({'C3'})),
        Profile('retention','https://workspace.invalid','producer'), lambda ref:ref, transport=loss)
    owner = make_owner(platform, exporter.resolver, executor=exporter.execution)
    app = create_app(settings=platform.settings, platform=platform, repository_run_owner=owner, candidate_publication_service=service)
    path = '/api/repository-runs/v1/runs/'+job.id+'/candidate-retention'
    body = {'workspace_id':'workspace-1', 'attempt_id':attempt, 'release_id':approved}
    async def scenario():
        loss.pause = 'knowledge.artifact.begin-v1'
        loss.entered, loss.resume = asyncio.Event(), asyncio.Event()
        async with httpx.AsyncClient(base_url='https://sovite.invalid', transport=httpx.ASGITransport(app=app),
                                     headers={'Authorization':'Bearer '+CALLER}) as c:
            first = asyncio.create_task(c.post(path, json=body))
            try:
                await asyncio.wait_for(loss.entered.wait(), timeout=10)
                second = await asyncio.wait_for(c.post(path, json=body), timeout=10)
                assert second.status_code == 409  # bounded, no event-loop deadlock
            finally:
                loss.resume.set()
            accepted = await asyncio.wait_for(first, timeout=10)
            assert accepted.status_code == 200, accepted.text
            replay = await c.post(path, json=body)
            assert replay.status_code == 200 and replay.json() == accepted.json()
        assert conn.execute(f'SELECT count(*) FROM "{schema}".artifact_version').fetchone()[0] == 1
        assert len([e for e in platform.get_job(job.id).resolved['repository_run']['evidence'] if e['label']=='retained-candidate']) == 1
    asyncio.run(scenario())
