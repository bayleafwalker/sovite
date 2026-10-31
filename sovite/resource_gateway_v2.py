"""Closed resource-owner gateway using existing durable local operation records."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ssl
import time

import httpx

from .repository_runtime import protected_bytes
from .resource_contract import runtime as contract
from .resource_gateway_read import _https_origin
from .resource_gateway_store import canonical
from .resource_target_catalog import (OPERATIONS,decode,installed_public_bytes,need,profile,
                                     select_entry,validate_policy)
from .resource_target_executors import FixedTargetExecutors


class OwnerClientV2:
    """One fixed HTTPS owner, only the three installed gateway operations."""
    def __init__(self,origin,*,ca_file,token_file):
        need(_https_origin(origin),'resource_owner_configuration_invalid')
        self.origin,self.ca_file,self.token_file=origin,ca_file,token_file
        self.ca=installed_public_bytes(ca_file,limit=65536)
        self.token=protected_bytes(token_file,limit=65536).decode().strip()
        need(bool(self.token) and '\n' not in self.token,'resource_owner_configuration_invalid')

    async def call(self,action,body):
        need(action in {'poll','current','result'})
        contract.validate_request(action,body)
        contract.load_bundle().validate_parameter(contract.OPERATIONS[action],'header','Authorization','Bearer '+self.token)
        need(installed_public_bytes(self.ca_file,limit=65536)==self.ca
             and protected_bytes(self.token_file,limit=65536).decode().strip()==self.token,
             'resource_owner_configuration_changed')
        tls=ssl.create_default_context(cadata=self.ca.decode('ascii'))
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(verify=tls,trust_env=False,follow_redirects=False,timeout=5) as client:
                async with client.stream('POST',self.origin+'/internal/resource-owner/v2/'+action,
                    headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},
                    content=canonical(body)) as response:
                    limit=1048576 if action=='poll' else 16384
                    raw=bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part);need(len(raw)<=limit,'resource_owner_invalid')
                    value=decode(raw)
                    contract.validate_response(action,response.status_code,value)
                    need(response.status_code==200,'resource_owner_denied')
                    return value


class GatewayV2:
    def __init__(self,store,owner,catalog,executors=None):
        self.store,self.owner,self.catalog=store,owner,catalog
        self.executors=executors or FixedTargetExecutors()
        self.cursor=None

    def _policy(self,packet):
        policy=validate_policy(self.store.policy(),self.catalog)
        need(time.time()<min(policy['expires_at'],packet['deadline']),'resource_authorization_denied')
        need(all(packet[key]==policy[key] for key in
             ('gateway_id','audience','tenant_id','principal_id','workspace_id','domain_id')),
             'resource_authorization_denied')
        selector=packet['request']['resource'];entry=select_entry(policy['catalog'],selector)
        need(entry==packet['target'] and entry['target_revision']==packet['request']['target_revision'],
             'resource_authorization_denied')
        return policy,entry,self.catalog.resolve(entry['target_id'],entry['target_revision'])

    async def process(self,packet):
        contract.validate_schema('GatewayRequest',packet)
        policy,entry,target=self._policy(packet)
        selector=packet['request']['resource'];operation=selector['operation']
        declared=profile(entry,operation)
        binding={'request_id':packet['request_id'],'fence_fingerprint':packet['fence_fingerprint'],
            'policy_revision':policy['release_policy_revision'],'destination':declared['destination'],
            'actor':policy['principal_id']}
        async def current():
            try:
                before=self._policy(packet)[0]==policy
                reply=await self.owner.call('current',binding) if before else None
                return reply=={'current':True} and self._policy(packet)[0]==policy
            except Exception:return False
        need(await current(),'resource_authorization_denied')
        effect=None
        if entry['kind']=='kvm-lab':
            verb=operation.split('.')[1]
            if verb=='create':
                need(packet.get('create') is None)
                effect={'schema_version':'cloud.kvm-lab-control.v1','verb':verb,
                    'operation_id':packet['request_id'],'create_operation_id':packet['request_id'],
                    'template_id':target['template_id'],'template_sha256':target['template_sha256'],
                    'deadline':min(packet['created_at']+target['max_ttl_seconds'],
                        packet['request']['execution_deadline'],policy['expires_at'])}
            else:
                original=self.store.lookup(selector['create_request_id'])
                need(original is not None,'resource_create_unavailable')
                prior=original['request'];source=prior['packet'];effect=dict(prior['effect'])
                create=packet.get('create');need(create is not None)
                need(source['request']['resource']=={'operation':'kvm-lab.create','target_id':selector['target_id']}
                     and source['request_id']==selector['create_request_id']
                     and all(source['target'][key]==entry[key] for key in ('kind','target_id','target_revision'))
                     and all(source[key]==packet[key] for key in
                         ('gateway_id','tenant_id','principal_id','workspace_id','domain_id'))
                     and create['create_request_id']==source['request_id']
                     and create['created_at']==source['created_at']
                     and create['execution_deadline']==source['request']['execution_deadline'],
                     'resource_create_unavailable')
                effect.update(verb=verb,operation_id=packet['request_id'])
            contract.validate_schema('LabHostRequest',effect)
        intent={'packet':packet,'effect':effect,'policy_sha256':hashlib.sha256(canonical(policy)).hexdigest()}
        admission=self.store.admit(packet['request_id'],intent)
        if admission['state']=='unknown':return {'state':'unknown'}
        metadata={**binding,'policy_sha256':intent['policy_sha256']}
        if admission['state']=='admitted':
            need(await current(),'resource_authorization_denied')
            common={'workspace_id':packet['workspace_id'],'resource_domain_id':packet['domain_id'],
                    'target_id':selector['target_id'],'operation':operation,'schema_version':OPERATIONS[operation][3]}
            if entry['kind']=='model-peer-health':
                need(await self.executors.health(target),'resource_health_unavailable')
                value={**common,'health_scope':'peer-http-daemon','daemon_responsive':True}
            else:
                try:state=(await self.executors.lab(effect))['state']
                except Exception:state='unknown'
                value={**common,'lab_id':effect['create_operation_id'],'state':state,
                    'effect_outcome':'unknown' if state=='unknown' else 'known','deadline':effect['deadline']}
            contract.validate_schema(OPERATIONS[operation][2],value)
            content=canonical(value)
            allowed=await current()
            self.store.observe(packet['request_id'],content,'allowed' if allowed else 'denied',metadata)
            need(allowed,'resource_authorization_denied')
        else:
            content=admission['observation']
            need(admission['release']=='allowed' and admission['release_metadata']==metadata,
                 'resource_release_denied')
            contract.validate_schema(OPERATIONS[operation][2],decode(content))
        need(await current(),'resource_authorization_denied')
        return await self.owner.call('result',{**binding,'observation_digest':'sha256:'+hashlib.sha256(content).hexdigest(),
            'observation_b64':base64.b64encode(content).decode(),'release':'allowed'})

    async def poll_once(self):
        value=await self.owner.call('poll',{'after':self.cursor})
        contract.validate_response('poll',200,value)
        self.cursor=value['next_cursor'];result=[]
        for packet in value['requests']:
            try:result.append(await self.process(packet))
            except Exception:result.append({'state':'unavailable'})
        return result
