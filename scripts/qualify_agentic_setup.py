#!/usr/bin/env python3
"""Real local setup-to-private-result assessment; never substitutes synthetic inference.

Uses an explicitly supplied loopback Ollama endpoint and a C0 synthetic source fixture.
Owns disposable API/worker state. This is not hosted or production qualification and
never claims Vuoro work acceptance. No credentials or result body enter the summary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovite import policy
from sovite.qualification import initialize_keys, issue_qualification, runtime_settings_fingerprint
from sovite.service import Platform
from sovite.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-endpoint', required=True)
    parser.add_argument('--expected-model-digest', required=True)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--private-artifact', required=True, type=Path)
    args = parser.parse_args()
    endpoint = urlsplit(args.model_endpoint)
    if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or not endpoint.port or endpoint.path or endpoint.query or endpoint.fragment or endpoint.username:
        parser.error('This local assessment requires an explicit loopback model endpoint')
    raw = args.fixture.read_bytes()
    fixture = json.loads(raw)
    if fixture.get('classification') != 'C0' or fixture.get('synthetic') is not True:
        parser.error('This assessment accepts only an explicitly public synthetic source fixture')
    if args.output.exists() or args.private_artifact.exists():
        parser.error('Output already exists; preserve each attempted run separately')
    started = time.time()
    model_rows = httpx.get(args.model_endpoint + '/api/tags', timeout=10).raise_for_status().json()['models']
    observed = next(m for m in model_rows if m['digest'] == args.expected_model_digest)
    version = httpx.get(args.model_endpoint + '/api/version', timeout=10).raise_for_status().json()
    with tempfile.TemporaryDirectory(prefix='sovite-agentic-real-') as directory:
        work = Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        operator, caller = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        bundle, _ = policy.load_bundle(ROOT / 'config/platform.ollama.yaml')
        bundle['metadata']['name'] = 'bayleaf-wf01'
        for model in bundle['models']:
            model['endpoint'] = args.model_endpoint + ('/v1' if model.get('wire_api') else '')
            if 'identity_endpoint' in model:
                model['identity_endpoint'] = args.model_endpoint
        (work / 'bundle.yaml').write_text(yaml.safe_dump(bundle))
        settings = Settings(database_url=f'sqlite:///{work}/control.db', bundle_path=str(work / 'bundle.yaml'),
            artifact_path=str(work / 'artifacts'), operator_token=operator, caller_tokens={'cloud-codex': caller},
            public_origin=origin, secure_cookies=False, setup_api_enabled=True, setup_installation_id='bayleaf-wf01',
            qualification_private_key=str(work / 'signer.pem'), qualification_public_keys=[str(work / 'public.pem')])
        platform = Platform(settings)
        platform.initialize()
        initialize_keys(settings)
        env = {**{key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}, 'SOVITE_DATABASE_URL': settings.database_url, 'SOVITE_DATABASE_MIGRATE': 'never',
            'SOVITE_DATABASE_SCHEMA': '', 'SOVITE_BUNDLE_PATH': settings.bundle_path, 'SOVITE_ARTIFACT_PATH': settings.artifact_path,
            'SOVITE_ARTIFACT_BACKEND': 'filesystem', 'SOVITE_OPERATOR_TOKEN': operator,
            'SOVITE_CALLER_TOKENS': json.dumps(settings.caller_tokens), 'SOVITE_PUBLIC_ORIGIN': origin,
            'SOVITE_SECURE_COOKIES': 'false', 'SOVITE_SETUP_API_ENABLED': 'true', 'SOVITE_SETUP_INSTALLATION_ID': 'bayleaf-wf01',
            'SOVITE_REQUIRE_QUALIFICATION': 'true', 'SOVITE_QUALIFICATION_PUBLIC_KEYS': json.dumps(settings.qualification_public_keys),
            'SOVITE_EMBEDDED_WORKER': 'false', 'SOVITE_COMPUTE_ID': 'local', 'SOVITE_POLL_SECONDS': '0.2',
            'SOVITE_DEPLOYMENT_BINDING': '', 'SOVITE_LEASE_SECONDS': '90'}
        env.pop('SOVITE_QUALIFICATION_PRIVATE_KEY', None)
        children = []
        with (work / 'processes.log').open('wb') as log:
            try:
                command = [sys.executable, '-c', 'from sovite.cli import main; main()']
                children.append(subprocess.Popen(command + ['serve', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=log, stderr=log))
                with httpx.Client(base_url=origin, timeout=15) as client:
                    for _ in range(100):
                        try:
                            if client.get('/health/ready').status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Owned control API did not become ready')
                    def headers(token):
                        return {'Authorization': 'Bearer ' + token}
                    def post(path, body, token=operator):
                        return client.post(path, json=body, headers=headers(token))
                    model = {'id': 'maintenance-model', 'name': 'Maintenance Qwen', 'adapter': 'ollama',
                        'endpoint': args.model_endpoint, 'model': observed['name'], 'destination': 'private-runtime',
                        'digest': 'sha256:' + observed['digest'], 'parameters': {'temperature': 0, 'think': False, 'num_predict': 768, 'num_ctx': 4096}}
                    principal = post('/api/setup/principals', {'name': 'bayleaf setup agent', 'ttl_seconds': 3600,
                        'envelope': {'models': [model], 'harnesses': ['private-analysis'], 'grants': ['incident-forensics'], 'max_steps': 2}}).raise_for_status().json()
                    agent_token = principal.pop('token')
                    context = client.get('/api/setup/context', headers=headers(agent_token)).raise_for_status().json()
                    draft = post('/api/setup/drafts', {'idempotency_key': 'wf01-setup', 'base_generation': context['generation'],
                        'model': model, 'agent_id': 'maintenance-helper', 'agent_name': 'Maintenance handover helper',
                        'harness': 'private-analysis', 'instructions': 'Prepare a concise maintenance handover using only supplied observations. Cite M1, M2 and M3. Separate facts, hypotheses and proposed checks. Owners remain unassigned. State no repair or outcome was verified. Do not invent causes, people, costs or deadlines.',
                        'max_steps': 2, 'grant': 'incident-forensics'}, agent_token).raise_for_status().json()
                    activation_path = '/api/setup/drafts/' + draft['id'] + '/activate'
                    assert post(activation_path, {'generation': draft['generation']}, agent_token).status_code == 401
                    inspected = client.get('/api/setup/drafts/' + draft['id'], headers=headers(operator)).raise_for_status().json()
                    assert inspected['generation'] == draft['generation']
                    post(activation_path, {'generation': draft['generation']}).raise_for_status()
                    task = json.dumps(fixture, ensure_ascii=False)
                    rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'begin_task', 'arguments': {
                        'grant_id': 'incident-forensics', 'task': task, 'idempotency_key': 'wf01-task'}}}
                    blocked = post('/mcp', rpc, caller).raise_for_status().json()
                    assert blocked['result']['isError'] is True
                    owner_blocked = post('/api/jobs', {'grant_id': 'incident-forensics', 'task': task, 'idempotency_key': 'wf01-owner-probe'})
                    assert owner_blocked.status_code == 403
                    assert owner_blocked.json()['detail']['code'] == 'qualification_required'
                    # An operator assertion authorizes ONLY this disposable development assessment.
                    evidence = {'generation': draft['generation'], 'runtime_binding': runtime_settings_fingerprint(settings),
                        'profile': 'private', 'evidence_kind': 'development-integration-authorization',
                        'scope': 'Local C0 fixture assessment only. Model identity observed; no production isolation, hosted or Vuoro acceptance qualification.',
                        'checks': [{'name': 'observed-model-identity', 'status': 'passed'}, {'name': 'blocked-unqualified-dispatch', 'status': 'passed'}]}
                    qualification = issue_qualification(platform.db, settings, draft['generation'], 'private', evidence, valid_days=1,
                        issuer='wf01-development-operator')
                    accepted = post('/mcp', rpc, caller).raise_for_status().json()['result']['structuredContent']
                    assert set(accepted) == {'receipt', 'status'}
                    assert post('/mcp', rpc, caller).json()['result']['structuredContent']['receipt'] == accepted['receipt']
                    children.append(subprocess.Popen(command + ['worker'], cwd=ROOT, env=env, stdout=log, stderr=log))
                    for _ in range(900):
                        status_rpc = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'task_status', 'arguments': {'receipt': accepted['receipt']}}}
                        status = post('/mcp', status_rpc, caller).raise_for_status().json()['result']['structuredContent']
                        if status['status'] in {'available', 'unavailable'}:
                            break
                        time.sleep(1)
                    assert status['status'] == 'available', 'Real inference did not produce an available artifact'
                    jobs = client.get('/api/jobs', headers=headers(operator)).raise_for_status().json()['items']
                    job = next(j for j in jobs if j['grant_id'] == 'incident-forensics')
                    artifact_path = '/api/jobs/' + job['id'] + '/artifact'
                    assert client.get(artifact_path, headers=headers(caller)).status_code == 403
                    artifact = client.get(artifact_path, headers=headers(operator)).raise_for_status()
                    result_hash = hashlib.sha256(artifact.content).hexdigest()
                    # A fresh HTTP client independently re-reads the authoritative bytes.
                    with httpx.Client(base_url=origin, headers=headers(operator), timeout=15) as verifier:
                        exact = verifier.get(artifact_path).raise_for_status().content
                    assert hashlib.sha256(exact).hexdigest() == result_hash
                    args.private_artifact.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(args.private_artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as artifact_file:
                        artifact_file.write(exact)
                    answer = json.loads(exact)['answer']
                    assert answer.strip() and all(s['id'] in answer for s in fixture['snippets'])
                    summary = {'schema': 'sovite.agentic-real-assessment/v1', 'observed_at': time.time(),
                        'scope': 'Disposable local C0 source fixture. Actual inference; not semantic work acceptance or hosted qualification.',
                        'model': observed['name'], 'model_digest': observed['digest'], 'serving_version': version,
                        'source_sha256': hashlib.sha256(raw).hexdigest(), 'generation': draft['generation'],
                        'setup_principal': principal['id'], 'setup_draft': draft['id'], 'qualification_id': qualification.id,
                        'qualification_kind': evidence['evidence_kind'], 'receipt': accepted['receipt'], 'status': status['status'],
                        'artifact_sha256': result_hash, 'artifact_bytes': len(exact), 'answer_characters': len(answer),
                        'checks': {'agent_activation_refused': True, 'unqualified_task_blocked': True, 'qualified_retry_available': True,
                            'submission_retry_same_receipt': True, 'caller_artifact_refused': True, 'fresh_client_exact_hash_match': True,
                            'M1_M2_M3_references_present': True, 'audit_chain_valid': platform.db.verify_audit()['valid']},
                        'elapsed_seconds': round(time.time() - started, 3),
                        'remaining': ['Independent semantic verification and Vuoro work acceptance', 'Released owner-adapter composition',
                            'Two-session recovery and stale settlement refusal', 'Hosted qualification and rollout']}
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with args.output.open('x') as output:
                        json.dump(summary, output, indent=2)
                        output.write('\n')
                    os.chmod(args.output, 0o600)
                    print(json.dumps(summary, indent=2))
            finally:
                for child in children:
                    child.terminate()
                for child in children:
                    try:
                        child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()


if __name__ == '__main__':
    main()
