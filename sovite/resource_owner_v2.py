"""Closed resource catalog operations over the existing execution owner rows."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import secrets
import time
import uuid

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select

from .authority import execution_authority_current
from .db import (Job,ResourceGatewayEnrollment,ResourceOutputGrant,ResourceOwnerRequest,
                 ResourceWorkloadScope,WorkerExecutionLease)
from .flow import check_destination,class_ranks
from .lease_service import ExecutionContext,ExecutionPrincipal
from .remote_execution import RemoteExecution
from .resource_contract import runtime as contract
from .resource_gateway_store import canonical
from .resource_target_catalog import (OPERATIONS,ResourceTargetError,decode,entry_for_operation,need,profile,
                                     select_entry,validate_catalog)


class ResourceOwnerV2:
    def __init__(self,platform,settings):
        self.platform,self.settings=platform,settings

    def _token(self,identity):
        workload,digest=identity
        current=self.settings.execution_tokens.get(workload)
        need(bool(current) and secrets.compare_digest(hashlib.sha256(current.encode()).hexdigest(),digest),
             'resource_workload_denied',403)

    def _active(self,session,identity,execution):
        self._token(identity)
        remote=RemoteExecution(self.platform)
        principal=ExecutionPrincipal(identity[0],self.settings.compute_id)
        context=ExecutionContext(**execution)
        try:
            owner=remote._owner(principal,context)
            job,lease=remote._job_lease(session,principal,context,owner)
            remote._require_started(job,lease,time.time())
            bundle,_=self.platform.active(session)
            recipe=remote.leases._authorize_current(session,job,time.time())
            if (job.resolved.get('model',{}).get('connection',{}).get('kind')=='model-gateway'
                    and not execution_authority_current(self.platform,session,job,lease,identity[0],time.time())):
                raise ValueError
            remote._require_started(job,lease,time.time())
            self._token(identity)
            return job,lease,recipe,bundle
        except ResourceTargetError:raise
        except Exception:
            raise ResourceTargetError('resource_execution_scope_denied',403) from None

    @staticmethod
    def _scope(session,workload):
        scope=session.get(ResourceWorkloadScope,workload)
        need(scope is not None and scope.target_catalog is not None
             and scope.observation_id is None and scope.namespace is None and scope.pod_name is None,
             'resource_scope_denied',403)
        validate_catalog(scope.target_catalog)
        return scope

    @staticmethod
    def _flow(recipe,bundle,job,declared):
        ranks=class_ranks(bundle)
        try:
            receiving=min(ranks[recipe['grant']['input_class']],ranks[job.resolved['input_class']])
            need(ranks[declared['source_class']]<=receiving and ranks[declared['output_class']]<=receiving,
                 'resource_clearance_denied',403)
            destinations={row['id']:row for row in bundle['destinations']}
            check_destination(bundle,destinations[declared['destination']],declared['output_class'])
            for destination in recipe['destinations']:
                check_destination(bundle,destination,declared['source_class'])
        except ResourceTargetError:raise
        except Exception:
            raise ResourceTargetError('resource_clearance_denied',403) from None

    def scope_install(self,body):
        need(body['workload_id'] in self.settings.execution_tokens,'resource_workload_denied',422)
        catalog=validate_catalog(body['catalog'])
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row=session.get(ResourceWorkloadScope,body['workload_id'])
            if row is None:
                row=ResourceWorkloadScope(workload_id=body['workload_id']);session.add(row)
            for key in ('gateway_id','tenant_id','workspace_id','domain_id'):setattr(row,key,body[key])
            row.observation_id=row.namespace=row.pod_name=None
            row.target_catalog=catalog;row.updated_at=time.time()
        return {'workload_id':body['workload_id'],'updated':True}

    def output_grant(self,body):
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            scope=self._scope(session,body['workload_id'])
            need(body['workload_id'] in self.settings.execution_tokens and all(
                getattr(scope,key)==body[key] for key in ('tenant_id','workspace_id','domain_id')),
                'resource_scope_denied',422)
            entry=entry_for_operation(scope.target_catalog,body['target_id'],body['operation'])
            declared=profile(entry,body['operation'])
            row=ResourceOutputGrant(id=uuid.uuid4().hex,workload_id=scope.workload_id,
                gateway_id=scope.gateway_id,principal_id=body['principal_id'],tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id,domain_id=scope.domain_id,observation_id=body['target_id'],
                schema_version=OPERATIONS[body['operation']][3],destination=declared['destination'],
                policy_revision=body['policy_revision'],active=True,created_at=time.time())
            session.add(row)
        return {'grant_id':row.id,'schema_version':row.schema_version,'destination':row.destination,
                'policy_revision':row.policy_revision}

    @staticmethod
    def _create(session,row,selector,entry):
        if selector['operation'] not in {'kvm-lab.observe','kvm-lab.stop'}:return None
        original=session.get(ResourceOwnerRequest,selector['create_request_id'])
        need(original is not None and original.namespace is None and original.pod_name is None,
             'resource_create_not_found',404)
        value=original.request
        contract.validate_schema('ResourceRequest',value)
        need(value['resource']=={'operation':'kvm-lab.create','target_id':selector['target_id']}
            and value['target_revision']==entry['target_revision'] and all(getattr(original,key)==getattr(row,key)
                for key in ('gateway_id','tenant_id','workspace_id','domain_id','principal_id')),
            'resource_create_not_found',404)
        return {'create_request_id':original.id,'principal_id':original.principal_id,
            'gateway_id':original.gateway_id,'tenant_id':original.tenant_id,
            'workspace_id':original.workspace_id,'domain_id':original.domain_id,
            'target_id':selector['target_id'],'target_revision':entry['target_revision'],
            'created_at':original.created_at,'execution_deadline':value['execution_deadline']}

    def submit(self,body,identity):
        execution,selector=body['execution'],body['resource']
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            job,lease,recipe,bundle=self._active(session,identity,execution)
            scope=self._scope(session,identity[0]);entry=select_entry(scope.target_catalog,selector)
            self._flow(recipe,bundle,job,profile(entry,selector['operation']))
            request={'schema_version':'sovite-resource-request/v2','workspace_id':scope.workspace_id,
                'resource_domain_id':scope.domain_id,'resource':copy.deepcopy(selector),
                'target_revision':entry['target_revision'],'execution_deadline':lease.execution_deadline}
            request_id=hashlib.sha256(f"{job.id}:{execution['operation_id']}".encode()).hexdigest()
            prior=session.get(ResourceOwnerRequest,request_id)
            if prior is not None:
                need(prior.request==request and prior.instance_id==execution['instance_id']
                    and prior.generation==execution['generation'] and prior.fence==lease.fence
                    and prior.workload_token_digest==identity[1] and prior.principal_id==job.caller
                    and prior.namespace is None and prior.pod_name is None
                    and all(getattr(prior,key)==getattr(scope,key) for key in
                        ('gateway_id','tenant_id','workspace_id','domain_id')),'resource_operation_conflict')
                row=prior
            else:
                now=time.time()
                row=ResourceOwnerRequest(id=request_id,job_id=job.id,instance_id=execution['instance_id'],
                    generation=execution['generation'],workload_id=identity[0],gateway_id=scope.gateway_id,
                    workload_token_digest=identity[1],principal_id=job.caller,fence=lease.fence,
                    tenant_id=scope.tenant_id,workspace_id=scope.workspace_id,domain_id=scope.domain_id,
                    observation_id=selector['target_id'],namespace=None,pod_name=None,request=request,
                    state='pending',created_at=now,updated_at=now)
                self._create(session,row,selector,entry)
                session.add(row)
            RemoteExecution(self.platform)._require_started(job,lease,time.time());self._token(identity)
            return {'request_id':row.id,'state':row.state,
                'fence_fingerprint':'sha256:'+hashlib.sha256(row.fence.encode()).hexdigest()}

    def _valid(self,session,row,gateway_identity,fingerprint):
        need(row is not None and row.namespace is None and row.pod_name is None,'resource_scope_denied',403)
        contract.validate_schema('ResourceRequest',row.request)
        enrollment=session.get(ResourceGatewayEnrollment,gateway_identity[0])
        need(enrollment is not None and not enrollment.revoked and enrollment.expires_at>time.time()
            and secrets.compare_digest(enrollment.token_digest,gateway_identity[1]) and enrollment.audience=='resource-gateway'
            and all(getattr(enrollment,key)==getattr(row,key) for key in
                ('gateway_id','tenant_id','workspace_id','domain_id')),'resource_gateway_denied',403)
        token=self.settings.execution_tokens.get(row.workload_id)
        need(token is not None and secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),row.workload_token_digest),
             'resource_workload_denied',403)
        execution={'instance_id':row.instance_id,'operation_id':'resource-owner-current',
            'job_id':row.job_id,'generation':row.generation,'fence':row.fence}
        job,lease,recipe,bundle=self._active(session,(row.workload_id,row.workload_token_digest),execution)
        scope=self._scope(session,row.workload_id)
        selector=row.request['resource'];entry=select_entry(scope.target_catalog,selector)
        need(all(getattr(scope,key)==getattr(row,key) for key in ('gateway_id','tenant_id','workspace_id','domain_id'))
            and row.principal_id==job.caller and row.observation_id==selector['target_id']
            and row.request['workspace_id']==row.workspace_id and row.request['resource_domain_id']==row.domain_id
            and row.request['target_revision']==entry['target_revision']
            and row.request['execution_deadline']==lease.execution_deadline
            and fingerprint=='sha256:'+hashlib.sha256(row.fence.encode()).hexdigest(),
            'resource_scope_denied',403)
        self._flow(recipe,bundle,job,profile(entry,selector['operation']))
        original=self._create(session,row,selector,entry)
        RemoteExecution(self.platform)._require_started(job,lease,time.time())
        self._token((row.workload_id,row.workload_token_digest))
        need(enrollment.expires_at>time.time() and not enrollment.revoked
             and secrets.compare_digest(enrollment.token_digest,gateway_identity[1]),'resource_gateway_denied',403)
        return entry,original

    @staticmethod
    def _grant(session,row,entry,body):
        declared=profile(entry,row.request['resource']['operation'])
        if body['destination']!=declared['destination'] or body['actor']!=row.principal_id:return None
        return session.scalars(select(ResourceOutputGrant).where(ResourceOutputGrant.active.is_(True),
            ResourceOutputGrant.workload_id==row.workload_id,ResourceOutputGrant.gateway_id==row.gateway_id,
            ResourceOutputGrant.principal_id==row.principal_id,ResourceOutputGrant.tenant_id==row.tenant_id,
            ResourceOutputGrant.workspace_id==row.workspace_id,ResourceOutputGrant.domain_id==row.domain_id,
            ResourceOutputGrant.observation_id==row.observation_id,
            ResourceOutputGrant.schema_version==OPERATIONS[row.request['resource']['operation']][3],
            ResourceOutputGrant.destination==body['destination'],
            ResourceOutputGrant.policy_revision==body['policy_revision'])).first()

    def poll(self,body,identity):
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            enrollment=session.get(ResourceGatewayEnrollment,identity[0]);need(enrollment is not None)
            query=select(ResourceOwnerRequest).where(ResourceOwnerRequest.state=='pending',
                ResourceOwnerRequest.gateway_id==enrollment.gateway_id,
                ResourceOwnerRequest.tenant_id==enrollment.tenant_id,ResourceOwnerRequest.workspace_id==enrollment.workspace_id,
                ResourceOwnerRequest.domain_id==enrollment.domain_id,ResourceOwnerRequest.namespace.is_(None),
                ResourceOwnerRequest.pod_name.is_(None))
            if body.get('after') is not None:query=query.where(ResourceOwnerRequest.id>body['after'])
            found=[]
            batch=list(session.scalars(query.order_by(ResourceOwnerRequest.id).limit(100)))
            for row in batch:
                fingerprint='sha256:'+hashlib.sha256(row.fence.encode()).hexdigest()
                try:entry,original=self._valid(session,row,identity,fingerprint)
                except Exception:continue
                found.append({'request_id':row.id,'job_id':row.job_id,'principal_id':row.principal_id,
                    'audience':'resource-gateway','instance_id':row.instance_id,'generation':row.generation,
                    'gateway_id':row.gateway_id,'tenant_id':row.tenant_id,'workspace_id':row.workspace_id,
                    'domain_id':row.domain_id,'workload':row.workload_id,'fence_fingerprint':fingerprint,
                    'request':copy.deepcopy(row.request),'target':entry,**({'create':original} if original is not None else {}),
                    'created_at':row.created_at,'deadline':row.request['execution_deadline']})
            return {'requests':found,'next_cursor':batch[-1].id if len(batch)==100 else None}

    def current(self,body,identity):
        try:
            with self.platform.db.sessions.begin() as session:
                self.platform.db.authority_lock(session)
                row=session.get(ResourceOwnerRequest,body['request_id'])
                entry,_=self._valid(session,row,identity,body['fence_fingerprint'])
                need(self._grant(session,row,entry,body) is not None)
                self._valid(session,row,identity,body['fence_fingerprint'])
            return {'current':True}
        except Exception:return {'current':False}

    @staticmethod
    def _observation(row,content,original):
        need(len(content)<=4096,'resource_invalid_observation',422)
        operation=row.request['resource']['operation']
        try:
            value=decode(content)
            contract.validate_schema(OPERATIONS[operation][2],value)
        except Exception:
            raise ResourceTargetError('resource_invalid_observation',422) from None
        need(canonical(value)==content and all(value[key]==expected for key,expected in (
            ('workspace_id',row.workspace_id),('resource_domain_id',row.domain_id),
            ('target_id',row.observation_id),('operation',operation))), 'resource_invalid_observation',422)
        if operation!='model-peer.health-read':
            selector=row.request['resource']
            creator=row.id if operation=='kvm-lab.create' else selector['create_request_id']
            created=row.created_at if original is None else original['created_at']
            deadline=row.request['execution_deadline'] if original is None else original['execution_deadline']
            need(value['lab_id']==creator and value['deadline']<=min(created+300,deadline),
                 'resource_invalid_observation',422)
        return value

    def result(self,body,identity):
        try:content=base64.b64decode(body['observation_b64'],validate=True)
        except Exception:raise ResourceTargetError('resource_invalid_observation',422) from None
        need('sha256:'+hashlib.sha256(content).hexdigest()==body['observation_digest'],
             'resource_invalid_observation',422)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row=session.get(ResourceOwnerRequest,body['request_id'])
            try:entry,original=self._valid(session,row,identity,body['fence_fingerprint'])
            except Exception:return {'accepted':False,'state':'fenced'}
            self._observation(row,content,original)
            grant=self._grant(session,row,entry,body)
            if grant is None or body['release']!='allowed':return {'accepted':False,'state':'withheld'}
            self._valid(session,row,identity,body['fence_fingerprint'])
            retained={'observation':content.decode(),'observation_digest':body['observation_digest'][7:],
                'release':'allowed','enrollment_id':identity[0],'output_schema':grant.schema_version,
                'output_destination':grant.destination,'output_policy_revision':grant.policy_revision,
                'output_actor':body['actor']}
            if row.observation is not None:
                replay=all(getattr(row,key)==value for key,value in retained.items())
                return {'accepted':replay,'state':'stored' if replay else 'conflict'}
            for key,value in retained.items():setattr(row,key,value)
            row.state='observed';row.updated_at=time.time()
            return {'accepted':True,'state':'stored'}

    def read(self,body,identity):
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            execution=body['execution'];self._active(session,identity,execution)
            row=session.get(ResourceOwnerRequest,body['request_id'])
            need(row is not None and row.job_id==execution['job_id'] and row.workload_id==identity[0]
                and row.fence==execution['fence'] and row.observation is not None and row.release=='allowed'
                and row.enrollment_id is not None,'resource_result_withheld',404)
            enrollment=session.get(ResourceGatewayEnrollment,row.enrollment_id)
            need(enrollment is not None,'resource_result_withheld',404)
            identity_gateway=(enrollment.id,enrollment.token_digest)
            fingerprint='sha256:'+hashlib.sha256(row.fence.encode()).hexdigest()
            entry,original=self._valid(session,row,identity_gateway,fingerprint)
            release={'destination':row.output_destination,'actor':row.output_actor,
                     'policy_revision':row.output_policy_revision}
            need(self._grant(session,row,entry,release) is not None,'resource_result_withheld',404)
            content=row.observation.encode();observation=self._observation(row,content,original)
            need(hashlib.sha256(content).hexdigest()==row.observation_digest,'resource_result_withheld',404)
            self._valid(session,row,identity_gateway,fingerprint)
            return {'observation':observation,'sha256':'sha256:'+row.observation_digest,
                'output_class':profile(entry,row.request['resource']['operation'])['output_class'],
                'destination':row.output_destination,'policy_revision':row.output_policy_revision}


def install_routes(app,platform,settings,*,operator,workload,gateway):
    service=ResourceOwnerV2(platform,settings)
    def wrap(auth,code):
        def authenticate(authorization: str=Header(default='')):
            try:return auth(authorization)
            except Exception:raise HTTPException(403,detail={'code':code,'message':'Resource identity is not permitted'}) from None
        return authenticate
    operator_auth=wrap(operator,'resource_operator_required')
    workload_auth=wrap(workload,'resource_workload_denied')
    gateway_auth=wrap(gateway,'resource_gateway_denied')
    def endpoint(operation,handler,auth):
        async def invoke(request: Request,identity=Depends(auth)):
            try:
                need(not request.url.query,'resource_invalid_request',422)
                contract.load_bundle().validate_parameter(contract.OPERATIONS[operation],'header',
                    'Authorization',request.headers.get('authorization',''))
                limit=contract.load_bundle().manifest['operations'][contract.OPERATIONS[operation]]['requestMaxBytes']
                raw=bytearray()
                async for part in request.stream():
                    raw.extend(part);need(len(raw)<=limit,'resource_payload_too_large',413)
                body=decode(raw);contract.validate_request(operation,body)
            except ResourceTargetError as exc:
                raise HTTPException(exc.status,detail={'code':exc.code,'message':'Resource request is not permitted'}) from None
            except Exception:
                raise HTTPException(422,detail={'code':'resource_invalid_request','message':'The resource request does not match its owner contract'}) from None
            try:
                result=await asyncio.to_thread(handler,body,identity)
                contract.validate_response(operation,200,result)
                return result
            except ResourceTargetError as exc:
                raise HTTPException(exc.status,detail={'code':exc.code,'message':'Resource operation is not permitted or unavailable'}) from None
            except Exception:
                raise HTTPException(503,detail={'code':'resource_owner_unavailable','message':'The exact resource owner operation is unavailable'}) from None
        return invoke
    methods={'scope':('PUT','workload-scopes',lambda body,_:service.scope_install(body),operator_auth),
        'output_grant':('POST','output-grants',lambda body,_:service.output_grant(body),operator_auth),
        'submit':('POST','requests',service.submit,workload_auth),'poll':('POST','poll',service.poll,gateway_auth),
        'current':('POST','current',service.current,gateway_auth),'result':('POST','result',service.result,gateway_auth),
        'result/read':('POST','result/read',service.read,workload_auth)}
    for operation,(method,path,handler,auth) in methods.items():
        app.add_api_route('/internal/resource-owner/v2/'+path,endpoint(operation,handler,auth),
                         methods=[method],name=contract.OPERATIONS[operation])
