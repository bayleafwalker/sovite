"""Explicit installer and independently hosted gateway processes."""
import argparse
import asyncio
import json
import os
import sys
import time

from .adapters import AdapterRunner
from .resource_gateway_service import Gateway, OwnerClient, admin_app, validate_policy
from .resource_gateway_store import ResourceOperationStore


def status(code, **counts):
    """Emit fixed codes and bounded counts; never include upstream data."""
    print(json.dumps({'component': 'resource_gateway', 'code': code, **counts},
                     sort_keys=True, separators=(',', ':')), file=sys.stderr, flush=True)


async def poll_loop(gateway, *, sleep=asyncio.sleep, clock=time.monotonic):
    counts = {'poll_errors': 0, 'unavailable': 0, 'unknown': 0, 'refused': 0}
    last_report = None
    while True:
        try:
            results = await gateway.poll_once()
            for result in results:
                if result.get('state') == 'unknown':
                    counts['unknown'] += 1
                elif result.get('state') == 'unavailable':
                    counts['unavailable'] += 1
                elif result.get('accepted') is False:
                    counts['refused'] += 1
        except Exception:
            counts['poll_errors'] += 1
        counts = {key: min(value, 1_000_000) for key, value in counts.items()}
        now = clock()
        if any(counts.values()) and (last_report is None or now - last_report >= 60):
            status('poll_degraded', **counts)
            counts = dict.fromkeys(counts, 0)
            last_report = now
        await sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('command', choices=('install', 'admin', 'poll'))
    args = parser.parse_args()
    store = ResourceOperationStore(args.database)
    if args.command == 'install':
        try:
            store.install()
        except Exception:
            status('store_install_failed')
            raise SystemExit(1) from None
    elif args.command == 'admin':
        import uvicorn
        token = os.environ.get('SOVITE_RESOURCE_ADMIN_TOKEN', '')
        if not token:
            status('admin_identity_missing')
            raise SystemExit(1)
        uvicorn.run(admin_app(store, token), host='127.0.0.1', port=8765)
    else:
        try:
            policy = store.policy()
        except Exception:
            status('store_unavailable')
            raise SystemExit(1) from None
        if policy is None:
            status('resource_policy_missing')
            raise SystemExit(1)
        try:
            validate_policy(policy)
        except Exception:
            status('resource_policy_invalid')
            raise SystemExit(1) from None
        try:
            owner = OwnerClient(os.environ.get('SOVITE_RESOURCE_OWNER_ORIGIN', ''),
                                os.environ.get('SOVITE_RESOURCE_GATEWAY_TOKEN', ''),
                                ca_file=os.environ.get('SOVITE_RESOURCE_OWNER_CA', ''))
        except Exception:
            status('owner_configuration_invalid')
            raise SystemExit(1) from None
        gateway = Gateway(store, owner, AdapterRunner())
        asyncio.run(poll_loop(gateway))

if __name__ == '__main__':
    main()
