"""Real owner/repair transactions keep allowance distinct from charge dollars.

Executors are declared synthetic accounting fixtures. Workspace/repair source
admission and both SQL backends are real; no Go or other model is contacted.
"""
import asyncio
from dataclasses import replace
from decimal import Decimal
import os
import uuid

import pytest
from sqlalchemy import create_engine

from sovite.db import Base
from sovite.repository_runs import RepositoryRunAttemptExecutor
from sovite.service import Platform,PlatformError
from sovite.worker import Worker
from test_repository_candidate_export import prepared
from test_repository_candidate_retention import served_workspace
from test_repository_repairs import (configured,candidate_destinations,candidate_additional_contracts,
                                    candidate_model_destination)
from test_repository_runs import make_owner,packet,submit,git


def quota(reserved=.7,limit=1.):
    return {'unit':'opencode-go-allowance','reserved':reserved,'limit':limit,'actual':None}


@pytest.fixture(params=['sqlite','postgres'])
def owner_backend(prepared,request):
    platform,exporter,*_=prepared
    if request.param=='sqlite':
        yield prepared
        return
    url=os.environ.get('SOVITE_TEST_POSTGRES_URL')
    if not url:
        if os.environ.get('SOVITE_REPAIR_REQUIRE_PG')=='1':pytest.fail('Disposable PostgreSQL is required')
        pytest.skip('SOVITE_TEST_POSTGRES_URL requires disposable PostgreSQL')
    if url.startswith(('postgresql://','postgres://')):url='postgresql+psycopg://'+url.split('://',1)[1]
    admin=create_engine(url);schema='goquota_'+uuid.uuid4().hex
    with admin.begin() as connection:connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    postgres=Platform(replace(platform.settings,database_url=url,database_schema=schema));postgres.db.migrate()
    try:
        with platform.db.engine.connect() as source,postgres.db.engine.begin() as target:
            for table in reversed(Base.metadata.sorted_tables):target.execute(table.delete())
            for table in Base.metadata.sorted_tables:
                rows=[dict(row) for row in source.execute(table.select()).mappings()]
                if rows:target.execute(table.insert(),rows)
        exporter.platform=exporter.execution.platform=postgres
        yield (postgres,exporter,postgres.get_job(prepared[2].id),*prepared[3:])
    finally:
        postgres.db.engine.dispose()
        with admin.begin() as connection:connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


async def root_with_quota(prepared,*,pending=False,legacy=False):
    platform,exporter,_,_,mirror,base=prepared
    async def execute(worktree,goal,deadline,budget,authorize):
        budget.acquire_slot('root-call')
        budget.reserve_cost('root-call',.5,**({} if legacy else {'quota':quota()}))
        if not pending:
            kwargs={} if legacy else {'quota_actual':.6,'provider_observation':{
                'charge_usd':'0','list_cost_usd':'.15','allowance':'.6','allowance_unit':'opencode-go-allowance'}}
            budget.settle_cost('root-call',0.,**kwargs)
            budget.release_slot('root-call')
        (worktree/'src/main.txt').write_text('candidate\n')
        git('add','-A',cwd=worktree)
        git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','quota root',cwd=worktree)
    exporter.execution.executor=execute
    owner=make_owner(platform,exporter.resolver,executor=exporter.execution)
    created=submit(owner,packet(base),idempotency_key='quota-root-'+uuid.uuid4().hex)
    assert await Worker(platform,repository_run_executor=exporter.execution).run_once()
    job=platform.get_job(created['id']);assert job.status=='completed',job.error_code
    attempt=job.resolved['repository_run']['attempts'][-1]['id']
    return platform,exporter,job,attempt,mirror,base


def repair_executor(calls,*,reserved=.3,actual=.2,limit=1.):
    async def execute(worktree,goal,deadline,budget,authorize,*,repair_context=None):
        assert repair_context and repair_context['candidate_tip_sha']==git('rev-parse','HEAD',cwd=worktree)
        budget.acquire_slot('child-call')
        budget.reserve_cost('child-call',.5,quota=quota(reserved,limit))
        calls.append('synthetic-provider-operation')
        budget.settle_cost('child-call',0.,quota_actual=actual,provider_observation={
            'charge_usd':'0','list_cost_usd':str(actual/4),'allowance':str(actual),
            'allowance_unit':'opencode-go-allowance'})
        budget.release_slot('child-call')
        (worktree/'src/repair-marker.txt').write_text('candidate repair\n')
        git('add','-A',cwd=worktree)
        git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-m','quota child',cwd=worktree)
    execute.supports_repository_repair_context=True
    return execute


@pytest.mark.parametrize('case',['root-exhausted','child-cap-reset','missing-old-quota'])
def test_repair_cannot_reset_or_forget_root_allowance(owner_backend,served_workspace,tmp_path,case):
    async def scenario():
        root=await root_with_quota(owner_backend,legacy=case=='missing-old-quota')
        service,body,client=await configured(root,served_workspace,tmp_path)
        calls=[]
        service.owner.executor.executor=repair_executor(calls,reserved=.5 if case=='root-exhausted' else .3,
                                                        limit=2. if case=='child-cap-reset' else 1.)
        try:
            child=await service.submit(body,principal_id=root[2].caller,idempotency_key='quota-child-'+uuid.uuid4().hex)
            assert await Worker(root[0],repository_run_executor=service.owner.executor).run_once()
            job=root[0].get_job(child['run_id'])
            assert job.status=='blocked' and job.error_code=='execution_uncertain'
            assert calls==[] and job.resolved['repository_run']['budget_reservations']==[]
            original=root[0].get_job(root[2].id).resolved['repository_run']
            assert original['usage_usd']==0
            if case!='missing-old-quota':assert Decimal(original['budget_reservations'][0]['quota']['actual'])==Decimal('.6')
        finally:await client.__aexit__(None,None,None)
    asyncio.run(scenario())


def test_pending_quota_and_fee_are_not_released_by_repair_admission(owner_backend,served_workspace,tmp_path):
    async def scenario():
        root=await root_with_quota(owner_backend,pending=True)
        service,body,client=await configured(root,served_workspace,tmp_path)
        try:
            with pytest.raises(PlatformError) as refused:
                await service.submit(body,principal_id=root[2].caller,idempotency_key='quota-pending-'+uuid.uuid4().hex)
            assert refused.value.code=='repository_repair_usage_unknown'
            record=root[0].get_job(root[2].id).resolved['repository_run']['budget_reservations'][0]
            assert record['state']=='reserved' and record['actual_usd'] is None and record['quota']['actual'] is None
        finally:await client.__aexit__(None,None,None)
    asyncio.run(scenario())


def test_known_zero_charge_repairs_still_share_one_root_allowance(owner_backend,served_workspace,tmp_path):
    async def scenario():
        root=await root_with_quota(owner_backend)
        service,body,client=await configured(root,served_workspace,tmp_path/'first')
        calls=[];service.owner.executor.executor=repair_executor(calls)
        try:
            child=await service.submit(body,principal_id=root[2].caller,idempotency_key='quota-first-'+uuid.uuid4().hex)
            assert await Worker(root[0],repository_run_executor=service.owner.executor).run_once()
        finally:await client.__aexit__(None,None,None)
        child=root[0].get_job(child['run_id']);assert child.status=='completed',child.error_code
        assert calls==['synthetic-provider-operation'] and child.resolved['repository_run']['usage_usd']==0
        row=child.resolved['repository_run']['budget_reservations'][0]
        assert row['actual_usd']==0 and Decimal(row['quota']['actual'])==Decimal('.2')
        assert row['provider_observation']['list_cost_usd']!='0'
        next_parent=(root[0],root[1],child,child.resolved['repository_run']['attempts'][-1]['id'],root[4],root[5])
        successor,body,client=await configured(next_parent,served_workspace,tmp_path/'second')
        successor.owner.executor.executor=repair_executor(calls)
        try:
            grandchild=await successor.submit(body,principal_id=child.caller,idempotency_key='quota-second-'+uuid.uuid4().hex)
            assert await Worker(root[0],repository_run_executor=successor.owner.executor).run_once()
            stopped=root[0].get_job(grandchild['run_id'])
            assert stopped.status=='blocked' and calls==['synthetic-provider-operation']
        finally:await client.__aexit__(None,None,None)
    asyncio.run(scenario())
