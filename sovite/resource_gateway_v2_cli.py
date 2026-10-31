"""Fixed outbound gateway and root-SSH typed policy installation; no admin HTTP."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import stat
import sys

from .resource_gateway_cli import poll_loop,status
from .resource_gateway_store import ResourceOperationStore
from .resource_gateway_v2 import GatewayV2,OwnerClientV2
from .resource_target_catalog import InstalledCatalog,decode,need,validate_policy


def _database_identity(path):
    path=Path(path)
    need(path.is_absolute() and str(path)==os.path.normpath(str(path)))
    info=path.lstat();parent=path.parent.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid!=0 and not info.st_mode&0o077
         and stat.S_ISDIR(parent.st_mode) and parent.st_uid==info.st_uid and not parent.st_mode&0o077,
         'resource_store_identity_invalid')
    for component in path.parents:
        ancestor=component.lstat()
        sticky=ancestor.st_uid==0 and bool(ancestor.st_mode&stat.S_ISVTX)
        need(stat.S_ISDIR(ancestor.st_mode) and ancestor.st_uid in {0,info.st_uid}
             and (not ancestor.st_mode&0o022 or sticky),'resource_store_identity_invalid')
    return info


def _write_as_database_owner(path,policy,identity,catalog_path):
    # fork retains code, not an arbitrary executable/backend selected by stdin.
    # Credentials from root's shell are removed before dropping privilege.
    os.environ.clear();os.umask(0o077);os.setgroups([])
    os.setgid(identity.st_gid);os.setuid(identity.st_uid)
    current=Path(path).lstat()
    need((current.st_dev,current.st_ino)==(identity.st_dev,identity.st_ino),
         'resource_store_identity_changed')
    checked=validate_policy(policy,InstalledCatalog(catalog_path))
    ResourceOperationStore(path).set_policy(checked)


def install_policy(path,catalog,raw):
    need(os.geteuid()==0,'resource_operator_required',403)
    need(len(raw)<=65536,'resource_policy_too_large',413)
    policy=validate_policy(decode(raw),catalog)
    identity=_database_identity(path)
    child=os.fork()
    if child==0:
        try:
            _write_as_database_owner(path,policy,identity,catalog.path)
        except BaseException:os._exit(1)
        os._exit(0)
    _,result=os.waitpid(child,0)
    need(os.WIFEXITED(result) and os.WEXITSTATUS(result)==0,'resource_policy_install_failed')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',required=True)
    parser.add_argument('--catalog',required=True)
    parser.add_argument('command',choices=('install','catalog-check','policy-install','poll'))
    args=parser.parse_args()
    try:
        catalog=InstalledCatalog(args.catalog)
        store=ResourceOperationStore(args.database)
        if args.command=='install':store.install()
        elif args.command=='catalog-check':print(json.dumps({'targets':catalog.describe()},sort_keys=True,separators=(',',':')))
        elif args.command=='policy-install':install_policy(args.database,catalog,sys.stdin.buffer.read(65537))
        else:
            validate_policy(store.policy(),catalog)
            owner=OwnerClientV2(os.environ.get('SOVITE_RESOURCE_OWNER_ORIGIN',''),
                ca_file=os.environ.get('SOVITE_RESOURCE_OWNER_CA',''),
                token_file=os.environ.get('SOVITE_RESOURCE_GATEWAY_TOKEN_FILE',''))
            asyncio.run(poll_loop(GatewayV2(store,owner,catalog)))
    except KeyboardInterrupt:return
    except Exception:
        status('resource_v2_unavailable');raise SystemExit(1) from None


if __name__=='__main__':main()
