"""Explicit installer and independently hosted gateway processes."""
import argparse
import asyncio
import os

from .adapters import AdapterRunner
from .resource_gateway_service import Gateway, OwnerClient, admin_app
from .resource_gateway_store import ResourceOperationStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('command', choices=('install', 'admin', 'poll'))
    args = parser.parse_args()
    store = ResourceOperationStore(args.database)
    if args.command == 'install':
        store.install()
    elif args.command == 'admin':
        import uvicorn
        token = os.environ.get('SOVITE_RESOURCE_ADMIN_TOKEN', '')
        if not token:
            raise SystemExit('admin identity not configured')
        uvicorn.run(admin_app(store, token), host='127.0.0.1', port=8765)
    else:
        owner = OwnerClient(os.environ.get('SOVITE_RESOURCE_OWNER_ORIGIN', ''),
                            os.environ.get('SOVITE_RESOURCE_GATEWAY_TOKEN', ''),
                            ca_file=os.environ.get('SOVITE_RESOURCE_OWNER_CA', ''))
        gateway = Gateway(store, owner, AdapterRunner())
        async def loop():
            while True:
                try:
                    await gateway.poll_once()
                except Exception:
                    pass  # No credentials, target data or upstream body in logs.
                await asyncio.sleep(5)
        asyncio.run(loop())

if __name__ == '__main__':
    main()
