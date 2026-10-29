"""Uninstalled resource-owner pod gateway; its only routine channel is outbound."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import secrets
import time

import httpx
from fastapi import FastAPI, Header, HTTPException

from .adapters import AdapterRunner
from .resource_gateway_read import ResourceGatewayRead, ResourceReadError, _binding, _https_origin
from .resource_gateway_store import ResourceOperationStore, canonical


class OwnerClient:
    def __init__(self, origin: str, token: str, *, ca_file: str):
        if not _https_origin(origin) or not token or not ca_file:
            raise ValueError("owner_configuration_invalid")
        self.client = httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + token}, verify=ca_file, timeout=10, follow_redirects=False, trust_env=False)

    def call(self, action: str, body: dict) -> dict:
        response = self.client.post('/internal/resource-owner/v1/' + action, json=body)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError('owner_response_invalid')
        return value


def validate_policy(policy):
    required = {'gateway_id', 'audience', 'tenant_id', 'principal_id', 'expires_at', 'binding', 'release_policy_revision'}
    if not isinstance(policy, dict) or set(policy) != required:
        raise ValueError('invalid_policy')
    if any(not isinstance(policy[k], str) or not policy[k] for k in ('gateway_id', 'audience', 'tenant_id', 'principal_id')):
        raise ValueError('invalid_policy')
    if type(policy['expires_at']) not in (float, int) or not 0 < policy['expires_at'] < 1e12:
        raise ValueError('invalid_policy')
    if not isinstance(policy['release_policy_revision'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', policy['release_policy_revision']):
        raise ValueError('invalid_policy')
    b = policy['binding']
    if not isinstance(b, dict):
        raise ValueError('invalid_policy')
    try:
        request = {k: b[k] for k in ('workspace_id', 'resource_domain_id', 'observation_id')}
        _binding(b, request, {'tenant_id': policy['tenant_id']})
    except (KeyError, ResourceReadError):
        raise ValueError('invalid_policy') from None
    if b['source_class'] not in {'C1', 'C2'} or b['output_class'] != 'C1':
        raise ValueError('invalid_policy')
    return copy.deepcopy(policy)


def admin_app(store: ResourceOperationStore, admin_token: str) -> FastAPI:
    """Separately mounted/listened owner administration, disabled without token."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    @app.put('/admin/v1/policy')
    def put_policy(policy: dict, authorization: str = Header(default='')):
        if not admin_token or not secrets.compare_digest(authorization, 'Bearer ' + admin_token):
            raise HTTPException(403, 'denied')
        try:
            store.set_policy(validate_policy(policy))
        except (ValueError, RuntimeError):
            raise HTTPException(422, 'invalid_policy') from None
        return {'updated': True}
    return app


class Gateway:
    def __init__(self, store: ResourceOperationStore, owner: OwnerClient, runner: AdapterRunner):
        self.store, self.owner, self.runner = store, owner, runner
        self.cursor = None

    async def process(self, packet: dict) -> dict:
        policy = self.store.policy()
        if policy is None:
            raise ResourceReadError('policy_unavailable')
        policy = validate_policy(policy)
        b = policy['binding']
        match = {'gateway_id': policy['gateway_id'], 'audience': policy['audience'], 'tenant_id': policy['tenant_id'],
                 'principal_id': policy['principal_id'], 'workspace_id': b['workspace_id'],
                 'domain_id': b['resource_domain_id'], 'observation_id': b['observation_id']}
        if (not isinstance(packet, dict) or any(packet.get(k) != v for k, v in match.items())
                or type(packet.get('deadline')) not in (int, float) or packet['deadline'] <= time.time()
                or time.time() >= policy['expires_at']):
            raise ResourceReadError('authorization_denied')
        operation_id, fingerprint = packet.get('request_id'), packet.get('fence_fingerprint')
        if not isinstance(fingerprint, str) or len(fingerprint) != 71 or not fingerprint.startswith('sha256:'):
            raise ResourceReadError('invalid_request')
        def current():
            try:
                return (self.store.policy() == policy and time.time() < min(policy['expires_at'], packet['deadline']) and
                        self.owner.call('current', {'request_id': operation_id, 'fence_fingerprint': fingerprint,
                                                    'policy_revision': policy['release_policy_revision'],
                                                    'destination': b['destination'], 'actor': policy['principal_id']}) == {'current': True})
            except Exception:
                return False
        if not current():
            raise ResourceReadError('authorization_denied')
        admission = self.store.admit(operation_id, packet)
        if admission['state'] == 'unknown':
            return {'state': 'unknown'}
        metadata = {'policy_revision': policy['release_policy_revision'], 'destination': b['destination'],
                    'actor': policy['principal_id'], 'policy_digest': 'sha256:' + hashlib.sha256(canonical(policy)).hexdigest()}
        release_attempt = []
        def release(_context, _binding, content, digest):
            release_attempt.append(content)
            try:
                value = json.loads(content)
                expected = {'schema_version': 'sovite-resource-pod-observation/v1', 'workspace_id': b['workspace_id'], 'resource_domain_id': b['resource_domain_id'], 'observation_id': b['observation_id'], 'namespace': b['namespace'], 'pod_name': b['pod_name']}
                valid = (set(value) == set(expected) | {'phase'} and all(value.get(k) == v for k, v in expected.items()) and value['phase'] in {'Pending', 'Running', 'Succeeded', 'Failed', 'Unknown'} and canonical(value) == content and digest == 'sha256:' + hashlib.sha256(content).hexdigest())
                return bool(valid and current())
            except (ValueError, TypeError, KeyError):
                return False
        if admission['state'] == 'admitted':
            class Catalog:
                def resolve(self, _request):
                    return self_binding
            self_binding = b
            context = {'tenant_id': policy['tenant_id'], 'principal_id': policy['principal_id'],
                       'attempt_id': packet['job_id'], 'fence': fingerprint}
            seam = ResourceGatewayRead(Catalog(), lambda *_: current(), release, self.runner)
            try:
                result = await seam.read({k: b[k] for k in ('workspace_id', 'resource_domain_id', 'observation_id')}, context=context)
            except ResourceReadError as exc:
                if exc.code == 'release_denied' and release_attempt:
                    self.store.observe(operation_id, release_attempt[-1], 'denied', metadata)
                raise
            content = canonical(result['observation'])
            digest = 'sha256:' + hashlib.sha256(content).hexdigest()
            self.store.observe(operation_id, content, 'allowed', metadata)
        else:
            content, digest = admission['observation'], admission['sha256']
            if admission['release'] != 'allowed' or admission['release_metadata'] != metadata or not release(None, None, content, digest):
                raise ResourceReadError('release_denied')
        if not current() or not release(None, None, content, digest):
            raise ResourceReadError('authorization_denied')
        return self.owner.call('result', {'request_id': operation_id, 'fence_fingerprint': fingerprint,
                                         'observation_digest': digest, 'observation_b64': base64.b64encode(content).decode(),
                                         'release': 'allowed', **{k: metadata[k] for k in ('policy_revision', 'destination', 'actor')}})

    async def poll_once(self):
        response = self.owner.call('poll', {'after': self.cursor})
        requests = response.get('requests')
        if not isinstance(requests, list) or len(requests) > 100:
            raise ResourceReadError('owner_unavailable')
        cursor = response.get('next_cursor')
        if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r'[0-9a-f]{64}', cursor)):
            raise ResourceReadError('owner_unavailable')
        self.cursor = cursor
        results = []
        for packet in requests:
            try:
                results.append(await self.process(packet))
            except Exception:
                # One malformed, denied or unavailable operation must not
                # prevent other independently admitted requests progressing.
                results.append({'state': 'unavailable'})
        return results
