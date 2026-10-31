"""Actual owner read -> existing caller approval -> bounded repair HTTP flow."""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from test_repository_repairs import (prepared, served_workspace, candidate_model_destination,
                                     candidate_destinations, configured)
from test_repository_runs import CALLER, OTHER
from test_workflows import revision_ref
from sovite.api import create_app
from sovite.db import Job, MetadataRow
from sovite.repository_repairs import preview_id


@pytest.fixture
def candidate_additional_contracts():
    return [{'id':'repair-feedback', 'source_class':'C3', 'output_class':'C3',
             'destinations':['private-runtime'], 'approvers':['operator','cloud-codex'],
             'procedure_ref':revision_ref('trusted-repair-feedback')},
            {'id':'operator-feedback', 'source_class':'C3', 'output_class':'C3',
             'destinations':['private-runtime'], 'approvers':['operator'],
             'procedure_ref':revision_ref('separate-operator-feedback')}]


def counts(platform):
    with platform.db.sessions() as session:
        return (session.scalar(select(func.count()).select_from(Job)),
                session.scalar(select(func.count()).select_from(MetadataRow).where(MetadataRow.key.like('release_%'))))


@pytest.mark.parametrize('lost_path', ['/api/output-releases','/api/repository-runs/v1/repairs'])
def test_eligible_caller_reads_context_without_approval_then_exact_lost_reply_reconciles(prepared, served_workspace, tmp_path, lost_path):
    async def scenario():
        service, original, reader = await configured(prepared, served_workspace, tmp_path)
        platform = service.owner.platform
        app = create_app(platform.settings, platform, repository_run_owner=service.owner)
        app.state.repository_repair_service = service
        class Loss(httpx.AsyncBaseTransport):
            def __init__(self): self.inner = httpx.ASGITransport(app=app); self.lost = False
            async def handle_async_request(self, request):
                response = await self.inner.handle_async_request(request)
                if request.url.path == lost_path and response.status_code < 300 and not self.lost:
                    self.lost = True
                    raise httpx.ReadError('synthetic acknowledgement loss after owner committed')
                return response
        transport = Loss()
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver',
            headers={'Authorization':'Bearer '+CALLER}) as http:
            try:
                before = counts(platform)
                request = {'parent_run_id':original['parent_run_id'], 'feedback_contract_id':'repair-feedback'}
                response = await http.post('/api/repository-runs/v1/repair-feedback-context', json=request)
                assert response.status_code == 200, response.json()
                context = response.json()
                assert counts(platform) == before  # this read grants no approval or new job
                assert context['candidate_sha256'] == original['candidate_sha256']
                assert context['source_digest'] == 'sha256:'+platform.get_job(context['parent_run_id']).artifact_sha256
                feedback = {'schema_version':'sovite-repository-repair-feedback/v1',
                    'parent_run_id':context['parent_run_id'], 'candidate_export_sha256':context['candidate_sha256'],
                    'candidate_tip_sha':context['candidate_tip_sha'], 'result':'check_failed',
                    'feedback':'New qualified fixture check feedback.'}
                approval = {'source_job_id':context['parent_run_id'], 'source_digest':context['source_digest'],
                    'contract_id':context['contract_id'], 'expected_contract_revision':context['contract_revision'],
                    'expected_generation':context['expected_generation'], 'content':json.dumps(feedback),
                    'evidence_refs':[revision_ref('trusted-fixture-check-result')]}
                if lost_path == '/api/output-releases':
                    with pytest.raises(httpx.ReadError): await http.post('/api/output-releases', json=approval)
                response = await http.post('/api/output-releases', json=approval)
                assert response.status_code == 201, response.json()
                release = response.json()['release_id']
                assert (await http.post('/api/output-releases', json=approval)).json()['release_id'] == release
                body = dict(original, feedback_release_id=release)
                body['preview_id'] = preview_id('cloud-codex', body['parent_run_id'], body['candidate_sha256'], release, body['max_cost_usd'])
                headers = {'Idempotency-Key':'feedback-read-and-repair-0001'}
                if lost_path == '/api/repository-runs/v1/repairs':
                    with pytest.raises(httpx.ReadError): await http.post('/api/repository-runs/v1/repairs', json=body, headers=headers)
                response = await http.post('/api/repository-runs/v1/repairs', json=body, headers=headers)
                assert response.status_code == 201, response.json()
                assert (await http.post('/api/repository-runs/v1/repairs', json=body, headers=headers)).json() == response.json()
                after = counts(platform)
                assert after == (before[0]+1, before[1]+1)
                assert transport.lost
            finally:
                await reader.__aexit__(None,None,None)
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['outside-policy','foreign-caller','not-approver','unmodeled-field'])
def test_feedback_context_refuses_ineligible_or_changed_authority(prepared, served_workspace, tmp_path, fault):
    async def scenario():
        service, original, reader = await configured(prepared, served_workspace, tmp_path)
        platform = service.owner.platform
        app = create_app(platform.settings, platform, repository_run_owner=service.owner)
        app.state.repository_repair_service = service
        request = {'parent_run_id':original['parent_run_id'], 'feedback_contract_id':'repair-feedback'}
        token = CALLER
        expected = 409
        if fault == 'outside-policy': request['feedback_contract_id'] = 'candidate-export'
        if fault == 'foreign-caller': token, expected = OTHER, 404
        if fault == 'not-approver':
            service.policy = type(service.policy)(2, ('operator-feedback',))
            request['feedback_contract_id'] = 'operator-feedback'
            expected = 403
        if fault == 'unmodeled-field': request['caller'], expected = 'cloud-codex', 422
        before = counts(platform)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
            headers={'Authorization':'Bearer '+token}) as http:
            try:
                response = await http.post('/api/repository-runs/v1/repair-feedback-context', json=request)
                assert response.status_code == expected, response.json()
                assert counts(platform) == before
            finally:
                await reader.__aexit__(None,None,None)
    asyncio.run(scenario())
