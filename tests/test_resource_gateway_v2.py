"""Actual owner HTTP plus durable gateway: no real host or LAN calls."""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from sovite.db import ResourceOwnerRequest
from sovite.resource_contract import runtime as contract
from sovite.resource_gateway_store import ResourceOperationStore,canonical
from sovite.resource_gateway_v2 import GatewayV2
from sovite.resource_target_catalog import InstalledCatalog,ResourceTargetError
from test_execution_leases import owner
from test_migrations import database
from test_policy_v1alpha2 import bundle as gateway_bundle
from test_resource_owner_v2 import PREFIX,catalog,grant,setup,submit

pytestmark=pytest.mark.skipif(sys.version_info<(3,12),reason='Released owner contract runtime requires Python3.12')


class Owner:
    def __init__(self,f):self.f=f;self.lose=False;self.actions=[]
    async def call(self,action,body):
        self.actions.append(action)
        value=self.f['client'].post(PREFIX+action,headers=self.f['gateway'],json=body)
        assert value.status_code==200,value.json()
        contract.validate_response(action,200,value.json())
        if action=='result' and self.lose:
            self.lose=False;raise ConnectionError('fixture lost committed response')
        return value.json()


class Executors:
    def __init__(self):self.calls=[];self.unknown=False;self.on_effect=None
    async def health(self,target):
        self.calls.append(('health',target))
        if self.on_effect:self.on_effect()
        return True
    async def lab(self,request):
        self.calls.append(('lab',request))
        if self.on_effect:self.on_effect()
        if self.unknown:raise ConnectionError('fixture unknown after helper acceptance')
        return {'state':'stopped' if request['verb']=='stop' else 'running'}


def installed(tmp_path):
    path=tmp_path/'catalog.json'
    value={'schema_version':'sovite-resource-installed-catalog/v2','entries':[
        {'kind':'kvm-lab','target_id':'synthetic-lab','template_id':'diskless-kvm-lab-v1',
         'template_sha256':'f'*64,'max_ttl_seconds':60}]}
    path.write_bytes(canonical(value));path.chmod(0o600)
    return InstalledCatalog(path)


def fixture(owner,tmp_path):
    actual=installed(tmp_path)
    f=setup(owner)
    scope=copy.deepcopy(f['scope']);scope['catalog']['entries']=[catalog()['entries'][1]]
    scope['catalog']['entries'][0]['target_revision']=actual.describe()[0]['target_revision']
    assert f['client'].put(PREFIX+'workload-scopes',headers=f['admin'],json=scope).status_code==200
    policy={'schema_version':'sovite-resource-policy/v2','gateway_id':scope['gateway_id'],
        'audience':'resource-gateway','tenant_id':scope['tenant_id'],'principal_id':'operations',
        'workspace_id':scope['workspace_id'],'domain_id':scope['domain_id'],'expires_at':time.time()+120,
        'catalog':scope['catalog'],'release_policy_revision':'sha256:'+'a'*64}
    directory=tmp_path/'private';directory.mkdir(mode=0o700)
    store=ResourceOperationStore(directory/'operations');store.install();store.set_policy(policy)
    wire=Owner(f);effects=Executors();gateway=GatewayV2(store,wire,actual,effects)
    return f,store,wire,effects,gateway


def packet(f,request_id):
    rows=f['client'].post(PREFIX+'poll',headers=f['gateway'],json={}).json()['requests']
    return next(row for row in rows if row['request_id']==request_id)


def test_actual_owner_lost_delivery_ack_replays_bytes_not_create(owner,tmp_path):
    f,store,wire,effects,gateway=fixture(owner,tmp_path)
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    grant(f,'kvm-lab.create','synthetic-lab')
    selected_packet=packet(f,selected['request_id']);wire.lose=True
    with pytest.raises(ConnectionError):asyncio.run(gateway.process(selected_packet))
    assert len(effects.calls)==1
    restarted=GatewayV2(store,wire,gateway.catalog,effects)
    assert asyncio.run(restarted.process(selected_packet))=={'accepted':True,'state':'stored'}
    assert len(effects.calls)==1
    with owner.platform.db.sessions() as session:
        row=session.get(ResourceOwnerRequest,selected['request_id'])
        observation=json.loads(row.observation)
        assert observation['state']=='running' and observation['effect_outcome']=='known'
        assert not {'unit','host_boot_id','invocation_id','template_sha256'}&observation.keys()
    stop=submit(f,{'operation':'kvm-lab.stop','target_id':'synthetic-lab',
                   'create_request_id':selected['request_id']},'stop-1').json()
    grant(f,'kvm-lab.stop','synthetic-lab')
    assert asyncio.run(restarted.process(packet(f,stop['request_id'])))['accepted']
    assert effects.calls[-1][1]['deadline']==effects.calls[0][1]['deadline']
    assert effects.calls[-1][1]['create_operation_id']==selected['request_id']


def test_restart_after_admission_never_reissues_unknown_create(owner,tmp_path):
    f,store,wire,effects,gateway=fixture(owner,tmp_path)
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    grant(f,'kvm-lab.create','synthetic-lab');value=packet(f,selected['request_id'])
    original=store.observe
    def fail_after_effect(*args,**kwargs):raise OSError('fixture crash before receipt')
    store.observe=fail_after_effect
    with pytest.raises(OSError):asyncio.run(gateway.process(value))
    store.observe=original
    assert asyncio.run(GatewayV2(store,wire,gateway.catalog,effects).process(value))=={'state':'unknown'}
    assert len(effects.calls)==1
    follow=submit(f,{'operation':'kvm-lab.observe','target_id':'synthetic-lab',
                      'create_request_id':selected['request_id']},'observe-unknown').json()
    grant(f,'kvm-lab.observe','synthetic-lab')
    assert asyncio.run(gateway.process(packet(f,follow['request_id'])))['accepted']
    assert [row[1]['verb'] for row in effects.calls]==['create','observe']


def test_helper_unknown_is_explicit_and_does_not_complete_execution(owner,tmp_path):
    f,store,wire,effects,gateway=fixture(owner,tmp_path);effects.unknown=True
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    grant(f,'kvm-lab.create','synthetic-lab')
    assert asyncio.run(gateway.process(packet(f,selected['request_id'])))['accepted']
    with owner.platform.db.sessions() as session:
        row=session.get(ResourceOwnerRequest,selected['request_id'])
        assert json.loads(row.observation)['effect_outcome']=='unknown'
        from sovite.db import Job
        assert session.get(Job,row.job_id).status=='running'


def test_mid_effect_revocation_retains_denied_bytes_and_no_release(owner,tmp_path):
    f,store,wire,effects,gateway=fixture(owner,tmp_path)
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    receipt=grant(f,'kvm-lab.create','synthetic-lab')
    effects.on_effect=lambda:f['client'].delete('/internal/resource-owner/v1/output-grants/'+receipt['grant_id'],headers=f['admin'])
    with pytest.raises(ResourceTargetError):asyncio.run(gateway.process(packet(f,selected['request_id'])))
    assert len(effects.calls)==1 and 'result' not in wire.actions
    with owner.platform.db.sessions() as session:assert session.get(ResourceOwnerRequest,selected['request_id']).observation is None
    assert store.lookup(selected['request_id'])['state']=='observed'


def test_changed_local_policy_or_target_cannot_replay_old_effect(owner,tmp_path):
    f,store,wire,effects,gateway=fixture(owner,tmp_path)
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    grant(f,'kvm-lab.create','synthetic-lab');value=packet(f,selected['request_id'])
    changed=store.policy();changed['principal_id']='foreign';store.set_policy(changed)
    with pytest.raises(ResourceTargetError):asyncio.run(gateway.process(value))
    assert effects.calls==[] and store.lookup(selected['request_id']) is None


def test_policy_expiring_during_owner_current_read_never_admits_effect(owner,tmp_path):
    f,store,wire,effects,gateway=fixture(owner,tmp_path)
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    grant(f,'kvm-lab.create','synthetic-lab');value=packet(f,selected['request_id'])
    original=wire.call
    async def expires(action,body):
        result=await original(action,body)
        if action=='current':
            policy=store.policy();policy['expires_at']=time.time()-1;store.set_policy(policy)
        return result
    wire.call=expires
    with pytest.raises(ResourceTargetError):asyncio.run(gateway.process(value))
    assert effects.calls==[] and store.lookup(selected['request_id']) is None


def test_followup_uses_current_policy_without_extending_original_create_ttl(owner,tmp_path):
    f,store,wire,effects,gateway=fixture(owner,tmp_path)
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    grant(f,'kvm-lab.create','synthetic-lab')
    assert asyncio.run(gateway.process(packet(f,selected['request_id'])))['accepted']
    original_deadline=effects.calls[0][1]['deadline']
    current=store.policy();current['expires_at']+=60;current['release_policy_revision']='sha256:'+'b'*64
    store.set_policy(current)
    stopped=submit(f,{'operation':'kvm-lab.stop','target_id':'synthetic-lab',
                      'create_request_id':selected['request_id']},'stop-current-policy').json()
    body={'workload_id':'actual-workload','principal_id':'operations','tenant_id':f['scope']['tenant_id'],
        'workspace_id':f['scope']['workspace_id'],'domain_id':f['scope']['domain_id'],'target_id':'synthetic-lab',
        'operation':'kvm-lab.stop','policy_revision':current['release_policy_revision']}
    assert f['client'].post(PREFIX+'output-grants',headers=f['admin'],json=body).status_code==200
    assert asyncio.run(gateway.process(packet(f,stopped['request_id'])))['accepted']
    assert effects.calls[-1][1]['deadline']==original_deadline
