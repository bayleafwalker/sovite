"""Render deployment boundaries, including their rejected configurations."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / 'deploy/helm/sovite'
pytestmark = pytest.mark.skipif(not shutil.which('helm'), reason='Helm is required')


def render(tmp_path, values):
    path = tmp_path / 'values.yaml'
    path.write_text(yaml.safe_dump(values))
    return subprocess.run(['helm', 'template', 'sovite', str(CHART), '-n', 'sovite', '-f', str(path)],
                          capture_output=True, text=True)


def test_native_workspace_stays_read_only_and_ambiguous_sources_fail(tmp_path):
    values = {'nativeHarness': {'enabled': True, 'workspace': {'configMap': 'approved-input'}}}
    result = render(tmp_path, values)
    assert result.returncode == 0, result.stderr
    rows = list(yaml.safe_load_all(result.stdout))
    pod = next(r for r in rows if r['kind'] == 'Deployment' and r['metadata']['name'] == 'sovite-native-harness')['spec']['template']['spec']
    assert next(v for v in pod['volumes'] if v['name'] == 'approved-workspace')['configMap']['name'] == 'approved-input'
    assert next(v for v in pod['containers'][0]['volumeMounts'] if v['name'] == 'approved-workspace')['readOnly'] is True
    values['nativeHarness']['workspace']['existingClaim'] = 'second-input'
    assert render(tmp_path, values).returncode != 0
    values['nativeHarness']['workspace'].pop('existingClaim')
    values['nativeHarness']['enabled'] = False
    assert render(tmp_path, values).returncode != 0


def test_cilium_paths_have_exact_roles_and_ports_and_api_needs_binding(tmp_path):
    values = {'networkPolicy': {'ciliumAPIServer': True, 'controlIngress': {'ciliumGateway': True}},
              'kubernetesTools': {'enabled': True, 'apiServerCIDRs': ['192.0.2.10/32']}}
    result = render(tmp_path, values)
    assert result.returncode == 0, result.stderr
    rows = list(yaml.safe_load_all(result.stdout))
    policies = {r['metadata']['name']: r['spec'] for r in rows if r['kind'] == 'CiliumNetworkPolicy'}
    api = policies['sovite-kubernetes-api']
    assert api['endpointSelector']['matchLabels']['sovite.component'] == 'worker'
    assert api['egress'] == [{'toEntities': ['kube-apiserver'], 'toPorts': [{'ports': [{'port': '6443', 'protocol': 'TCP'}]}]}]
    gateway = policies['sovite-gateway-ingress']
    assert gateway['endpointSelector']['matchLabels']['sovite.component'] == 'control'
    assert gateway['ingress'] == [{'fromEntities': ['ingress'], 'toPorts': [{'ports': [{'port': '8080', 'protocol': 'TCP'}]}]}]
    values['kubernetesTools']['apiServerCIDRs'] = []
    assert render(tmp_path, values).returncode != 0


def test_worker_job_timeout_is_explicit_and_configurable(tmp_path):
    def worker_env(values):
        result = render(tmp_path, values)
        assert result.returncode == 0, result.stderr
        rows = list(yaml.safe_load_all(result.stdout))
        pod = next(r for r in rows if r and r['kind'] == 'Deployment' and r['metadata']['name'] == 'sovite-worker')['spec']['template']['spec']
        return {e['name']: e.get('value') for e in pod['containers'][0]['env']}
    assert worker_env({})['SOVITE_JOB_TIMEOUT_SECONDS'] == '300'
    assert worker_env({'worker': {'jobTimeoutSeconds': 900}})['SOVITE_JOB_TIMEOUT_SECONDS'] == '900'


def test_external_model_hosts_need_exact_cidrs_and_provisioned_credentials(tmp_path):
    peer = {'hosts': ['192.0.2.20'], 'cidrs': ['192.0.2.20/32'], 'ports': [8021]}
    values = {'networkPolicy': {'modelPeers': [peer]}, 'modelCredentials': [{'env': 'MODEL_HOST_TOKEN'}], 'nativeHarness': {'enabled': True}}
    result = render(tmp_path, values)
    assert result.returncode == 0, result.stderr
    rows = [r for r in yaml.safe_load_all(result.stdout) if r]
    policy = next(r for r in rows if r['metadata']['name'] == 'sovite-modelpeers-egress')['spec']
    assert policy['egress'] == [{'to': [{'ipBlock': {'cidr': '192.0.2.20/32'}}], 'ports': [{'protocol': 'TCP', 'port': 8021}]}]
    dns = next(r for r in rows if r['metadata']['name'] == 'sovite-private-dns')
    assert '192.0.2.20' not in yaml.safe_dump(dns)
    for name in ('sovite-worker', 'sovite-native-harness'):
        pod = next(r for r in rows if r['kind'] == 'Deployment' and r['metadata']['name'] == name)['spec']['template']['spec']
        env = {e['name']: e for e in pod['containers'][0]['env']}
        assert env['MODEL_HOST_TOKEN']['valueFrom']['secretKeyRef']['key'] == 'MODEL_HOST_TOKEN'
    for bad in ({'cidrs': ['192.0.2.0/24']}, {'cidrs': ['0.0.0.0/0']}, {'hosts': []},
                {'namespaceSelector': {'matchLabels': {'a': 'b'}}, 'podSelector': {'matchLabels': {'a': 'b'}}}):
        assert render(tmp_path, {'networkPolicy': {'modelPeers': [{**peer, **bad}]}}).returncode != 0
    for env in ('SOVITE_DATABASE_URL', 'AWS_SECRET_ACCESS_KEY', 'lower'):
        assert render(tmp_path, {'modelCredentials': [{'env': env}]}).returncode != 0


def test_contract_binds_external_model_to_its_cidr_and_credential():
    import sys
    sys.path.insert(0, str(CHART.parents[2] / 'scripts'))
    from deployment_contract import load_values, peer_allows, validate_bindings
    from sovite.policy import load_bundle
    assert peer_allows([{'hosts': ['192.0.2.20'], 'cidrs': ['192.0.2.20/32'], 'ports': [8021]}], '192.0.2.20', 8021)
    assert not peer_allows([{'hosts': ['models.example'], 'cidrs': ['192.0.2.20/32'], 'ports': [8021]}], 'models.example', 8021)
    assert not peer_allows([{'hosts': ['192.0.2.21'], 'cidrs': ['192.0.2.20/32'], 'ports': [8021]}], '192.0.2.21', 8021)
    assert not peer_allows([{'hosts': ['192.0.2.20'], 'cidrs': ['192.0.2.0/24'], 'ports': [8021]}], '192.0.2.20', 8021)
    bundle, _ = load_bundle(CHART.parents[2] / 'config/platform.kubernetes.yaml')
    native = next(m for m in bundle['models'] if m['id'] == 'native-reasoner')
    native.update(endpoint='https://192.0.2.20:8021/v1', identity_endpoint='https://192.0.2.20:8021', api_key_env='MODEL_HOST_TOKEN')
    values = load_values(CHART, [CHART.parents[2] / 'deploy/examples/reference-values.yaml'])
    values['kubernetesTools']['apiServerCIDRs'] = ['192.0.2.10/32']
    values['networkPolicy']['modelPeers'] = [{'hosts': ['192.0.2.20'], 'cidrs': ['192.0.2.20/32'], 'ports': [8021]}]
    with pytest.raises(ValueError, match='credential'):
        validate_bindings(bundle, values, 'sovite')
    values['modelCredentials'] = [{'env': 'MODEL_HOST_TOKEN'}]
    assert 'native-reasoner' in validate_bindings(bundle, values, 'sovite')['model_bindings']
    native['tls_trust'] = 'model-host'
    with pytest.raises(ValueError, match='trust anchor'):
        validate_bindings(bundle, values, 'sovite')
    values['modelTrust'] = {'model-host': '-----BEGIN CERTIFICATE-----\n'}
    assert 'native-reasoner' in validate_bindings(bundle, values, 'sovite')['model_bindings']
    native.update(endpoint='http://192.0.2.20:8021/v1', identity_endpoint='http://192.0.2.20:8021')
    with pytest.raises(ValueError, match='HTTPS'):
        validate_bindings(bundle, values, 'sovite')


def test_model_trust_is_mounted_where_models_are_called(tmp_path):
    pem = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n'
    result = render(tmp_path, {'modelTrust': {'model-host': pem}, 'nativeHarness': {'enabled': True}})
    assert result.returncode == 0, result.stderr
    rows = [r for r in yaml.safe_load_all(result.stdout) if r]
    trust = next(r for r in rows if r['kind'] == 'ConfigMap' and r['metadata']['name'] == 'sovite-model-trust')
    assert trust['data'] == {'model-host.pem': pem}
    for name in ('sovite-worker', 'sovite-native-harness'):
        pod = next(r for r in rows if r['kind'] == 'Deployment' and r['metadata']['name'] == name)['spec']['template']['spec']
        container = pod['containers'][0]
        assert {'name': 'SOVITE_MODEL_TRUST_DIR', 'value': '/model-trust'} in container['env']
        assert {'name': 'model-trust', 'mountPath': '/model-trust', 'readOnly': True} in container['volumeMounts']
        assert next(v for v in pod['volumes'] if v['name'] == 'model-trust')['configMap']['name'] == 'sovite-model-trust'
    assert 'SOVITE_MODEL_TRUST_DIR' not in render(tmp_path, {}).stdout
    assert render(tmp_path, {'modelTrust': {'Bad_Name': pem}}).returncode != 0
    assert render(tmp_path, {'modelTrust': {'model-host': 'not a certificate'}}).returncode != 0


def test_native_sandbox_needs_user_namespace_with_localhost_profile(tmp_path):
    def pod(values):
        result = render(tmp_path, values)
        assert result.returncode == 0, result.stderr
        rows = [r for r in yaml.safe_load_all(result.stdout) if r]
        return next(r for r in rows if r['kind'] == 'Deployment' and r['metadata']['name'] == 'sovite-native-harness')['spec']['template']['spec']
    default = pod({'nativeHarness': {'enabled': True}})
    assert 'hostUsers' not in default and default['securityContext']['seccompProfile'] == {'type': 'RuntimeDefault'}
    env = {e['name']: e.get('value') for e in default['containers'][0]['env']}
    assert env['SOVITE_NATIVE_HOME_ROOT'] == '/native-home'
    assert {'name': 'native-home', 'mountPath': '/native-home'} in default['containers'][0]['volumeMounts']
    sandbox = {'userNamespace': True, 'seccompLocalhostProfile': 'profiles/sovite-codex-sandbox.json'}
    enabled = pod({'nativeHarness': {'enabled': True, 'sandbox': sandbox}})
    assert enabled['hostUsers'] is False
    assert enabled['securityContext']['seccompProfile'] == {'type': 'Localhost', 'localhostProfile': 'profiles/sovite-codex-sandbox.json'}
    assert render(tmp_path, {'nativeHarness': {'enabled': True, 'sandbox': {'seccompLocalhostProfile': 'profiles/x.json'}}}).returncode != 0


def test_shipped_seccomp_profile_only_widens_namespace_syscalls():
    import json
    profile = json.loads((CHART.parents[2] / 'deploy/seccomp/sovite-codex-sandbox.json').read_text())
    assert profile['defaultAction'] == 'SCMP_ACT_ERRNO'
    assert not any('includes' in r or 'excludes' in r for r in profile['syscalls'])
    widened = profile['syscalls'][-1]
    assert widened == {'names': ['unshare', 'clone', 'setns', 'mount', 'umount2', 'pivot_root'], 'action': 'SCMP_ACT_ALLOW'}
    allowed = {n for r in profile['syscalls'][:-1] if r['action'] == 'SCMP_ACT_ALLOW' and not r.get('args') for n in r['names']}
    assert not allowed & {'unshare', 'setns', 'mount', 'umount2', 'pivot_root', 'bpf', 'init_module', 'kexec_load'}
