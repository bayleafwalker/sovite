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
