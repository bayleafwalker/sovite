"""Synthetic TLS services: actual execution owner, gateway and bounded target."""
import asyncio
import copy
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from sovite.adapters import AdapterRunner
from sovite.api import create_app
from sovite.resource_gateway_service import Gateway, OwnerClient
from sovite.resource_gateway_read import ResourceReadError
from sovite.resource_gateway_store import ResourceOperationStore
from test_execution_leases import owner  # noqa: F401
from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401
from test_resource_gateway_read import BINDING
from test_worker_remote_boundary import TOKEN, admit_agent_http_owner, pull_and_start


@contextmanager
def serve(app, cert, key):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off', ssl_certfile=str(cert), ssl_keyfile=str(key)))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(.01)
    assert server.started
    try:
        yield f'https://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()


def test_real_owner_gateway_and_target_over_tls(owner, tmp_path, monkeypatch):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    admit_agent_http_owner(owner)
    owner.platform.submit('native-code-review', 'synthetic', 'operations', 'gateway-tls', input_class='C2')
    settings = replace(owner.platform.settings, operator_token='synthetic-operator', execution_tokens={'actual-workload': TOKEN})
    app = create_app(settings=settings, platform=owner.platform)
    client = TestClient(app)
    job, identity, _ = pull_and_start(client, owner)
    admin = {'Authorization': 'Bearer synthetic-operator'}
    worker = {'Authorization': 'Bearer ' + TOKEN}
    scope = {'gateway_id': 'gateway-a', 'workload_id': 'actual-workload', 'tenant_id': 'tenant-a', 'workspace_id': 'workspace-a', 'domain_id': 'domain-a', 'observation_id': 'pod-health', 'namespace': 'application', 'pod_name': 'web-0'}
    prefix = '/internal/resource-owner/v1/'
    assert client.put(prefix + 'workload-scopes', headers=admin, json=scope).status_code == 200
    revision = 'sha256:' + 'a' * 64
    grant = {k: scope[k] for k in ('workload_id', 'tenant_id', 'workspace_id', 'domain_id', 'observation_id')} | {'principal_id': 'operations', 'policy_revision': revision}
    assert client.post(prefix + 'output-grants', headers=admin, json=grant).status_code == 200
    enrollment = client.post(prefix + 'enrollments', headers=admin, json={k: scope[k] for k in ('tenant_id', 'workspace_id', 'domain_id')} | {'gateway_id': 'gateway-a', 'expires_at': time.time() + 300})
    assert enrollment.status_code == 200
    submitted = client.post(prefix + 'requests', headers=worker, json=identity | {'operation_id': 'read-1'})
    assert submitted.status_code == 200
    request_id = submitted.json()['request_id']
    calls = []
    after_read = [lambda: None]
    target = FastAPI()
    @target.get('/api/v1/namespaces/application/pods')
    def pods(authorization: str = Header(default='')):
        if authorization != 'Bearer synthetic-target':
            raise HTTPException(403)
        calls.append(1)
        after_read[0]()
        return {'kind': 'PodList', 'items': [{'metadata': {'name': 'web-0', 'namespace': 'application'}, 'status': {'phase': 'Running', 'message': 'private'}}]}
    monkeypatch.setenv('SOVITE_TEST_READ_TOKEN', 'synthetic-target')
    with serve(target, cert, key) as target_origin, serve(app, cert, key) as owner_origin:
        binding = copy.deepcopy(BINDING)
        binding['tool'].update(endpoint=target_origin, ca_file=str(cert))
        policy = {'gateway_id': 'gateway-a', 'audience': 'resource-gateway', 'tenant_id': 'tenant-a', 'principal_id': 'operations', 'expires_at': time.time() + 300, 'binding': binding, 'release_policy_revision': revision}
        store = ResourceOperationStore(tmp_path / 'resource.db')
        store.install()
        store.set_policy(policy)
        gateway = Gateway(store, OwnerClient(owner_origin, enrollment.json()['bearer_token'], ca_file=str(cert)), AdapterRunner())
        packet = gateway.owner.call('poll', {})['requests'][0]
        real_call = gateway.owner.call
        def lost_response(action, body):
            result = real_call(action, body)
            if action == 'result':
                raise ConnectionError('synthetic lost response')
            return result
        gateway.owner.call = lost_response
        with pytest.raises(ConnectionError):
            asyncio.run(gateway.process(packet))
        assert len(calls) == 1
        # Restart gateway with its retained operation record and resend the same bytes.
        gateway = Gateway(ResourceOperationStore(tmp_path / 'resource.db'), OwnerClient(owner_origin, enrollment.json()['bearer_token'], ca_file=str(cert)), AdapterRunner())
        assert asyncio.run(gateway.process(packet)) == {'accepted': True, 'state': 'stored'}
        assert len(calls) == 1
        changed = dict(packet, observation_id='foreign')
        with pytest.raises(ResourceReadError):
            asyncio.run(gateway.process(changed))
        assert len(calls) == 1
        read = client.post(prefix + 'result/read', headers=worker, json=identity | {'operation_id': 'read-result', 'request_id': request_id})
        assert read.status_code == 200
        assert read.json()['observation']['phase'] == 'Running'
        assert 'private' not in read.text
        second = client.post(prefix + 'requests', headers=worker, json=identity | {'operation_id': 'read-2'})
        assert second.status_code == 200
        second_packet = gateway.owner.call('poll', {})['requests'][0]
        # Close the real owner connection only after the actual target responds.
        after_read[0] = gateway.owner.client.close
        with pytest.raises(ResourceReadError, match='authorization_denied'):
            asyncio.run(gateway.process(second_packet))
        assert len(calls) == 2
        gateway = Gateway(ResourceOperationStore(tmp_path / 'resource.db'), OwnerClient(owner_origin, enrollment.json()['bearer_token'], ca_file=str(cert)), AdapterRunner())
        assert asyncio.run(gateway.process(second_packet)) == {'state': 'unknown'}
        assert len(calls) == 2
        third = client.post(prefix + 'requests', headers=worker, json=identity | {'operation_id': 'read-3'})
        assert third.status_code == 200
        packet = next(p for p in gateway.owner.call('poll', {})['requests'] if p['request_id'] == third.json()['request_id'])
        gateway.owner.client.close()
        with pytest.raises(ResourceReadError, match='authorization_denied'):
            asyncio.run(gateway.process(packet))
        assert len(calls) == 2
    client.close()
