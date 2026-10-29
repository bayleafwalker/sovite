import copy
import time

import asyncio
import httpx

from sovite.resource_gateway_service import admin_app
from sovite.resource_gateway_store import ResourceOperationStore
from test_resource_gateway_read import BINDING


def test_separate_admin_is_disabled_by_default_and_requires_valid_policy(tmp_path):
    store = ResourceOperationStore(tmp_path / 'store')
    store.install()
    policy = {'gateway_id': 'gateway-a', 'audience': 'resource-gateway', 'tenant_id': 'tenant-a',
              'principal_id': 'alice', 'expires_at': time.time() + 100,
              'binding': copy.deepcopy(BINDING), 'release_policy_revision': 'sha256:' + 'a' * 64}
    async def exercise():
        headers = {'Authorization': 'Bearer synthetic-admin'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app(store, '')), base_url='http://local') as disabled:
            assert (await disabled.put('/admin/v1/policy', json=policy, headers={'Authorization': 'Bearer '})).status_code == 403
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app(store, 'synthetic-admin')), base_url='http://local') as client:
            assert (await client.put('/admin/v1/policy', json=policy)).status_code == 403
            bad = copy.deepcopy(policy)
            bad['binding']['output_class'] = 'C3'
            assert (await client.put('/admin/v1/policy', json=bad, headers=headers)).status_code == 422
            bad = copy.deepcopy(policy)
            bad['binding']['source_class'] = 'C3'
            assert (await client.put('/admin/v1/policy', json=bad, headers=headers)).status_code == 422
            assert store.policy() is None
            assert (await client.put('/admin/v1/policy', json=policy, headers=headers)).status_code == 200
            assert store.policy() == policy
    asyncio.run(exercise())
