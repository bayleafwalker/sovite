"""Closed installed resource targets; requests select identities, never URLs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat

from .resource_contract.runtime import validate_schema
from .resource_gateway_read import _https_origin
from .resource_gateway_store import canonical


OPERATIONS={
    'model-peer.health-read':('model-peer-health','read_profile','PeerHealthObservation','sovite-resource-peer-health/v1'),
    'kvm-lab.create':('kvm-lab','create_profile','LabCreated','sovite-resource-lab-created/v1'),
    'kvm-lab.observe':('kvm-lab','observe_profile','LabObserved','sovite-resource-lab-observed/v1'),
    'kvm-lab.stop':('kvm-lab','stop_profile','LabStopped','sovite-resource-lab-stopped/v1'),
}


class ResourceTargetError(ValueError):
    def __init__(self,code='resource_target_unavailable',status=409):
        self.code,self.status=code,status
        super().__init__(code)


def need(condition,code='resource_target_unavailable',status=409):
    if not condition:raise ResourceTargetError(code,status)


def unique_pairs(pairs):
    result={}
    for key,value in pairs:
        need(key not in result,'resource_invalid_request',422)
        result[key]=value
    return result


def decode(raw):
    return json.loads(raw,object_pairs_hook=unique_pairs,
        parse_constant=lambda _:need(False,'resource_invalid_request',422))


def installed_public_bytes(value,*,limit=1024*1024):
    """No-follow public configuration/CA, not a credential-reading API.

    Root/service ownership and no unrelated writer are required throughout.
    Public Nix store files need not have secret-file permissions. Candidates
    run outside the gateway UID; that trusted component owns local policy.
    """
    path=Path(value)
    need(path.is_absolute() and path==Path(os.path.normpath(path)))
    parts=path.parts[1:];need(bool(parts))
    directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=child
            info=os.fstat(directory)
            sticky=info.st_uid==0 and bool(info.st_mode&stat.S_ISVTX)
            need(info.st_uid in {0,os.geteuid()} and (not info.st_mode&0o022 or sticky))
        parent=os.fstat(directory);need(not parent.st_mode&0o022)
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        try:
            info=os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid in {0,os.geteuid()}
                and not info.st_mode&0o022 and info.st_size<=limit)
            with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(limit+1)
            need(len(raw)<=limit)
            return raw
        finally:os.close(fd)
    finally:os.close(directory)


def validate_catalog(value):
    validate_schema('TargetCatalog',value)
    entries=value['entries']
    need(len({row['target_id'] for row in entries})==len(entries),'resource_catalog_invalid',422)
    need(len({row['kind'] for row in entries})==len(entries),'resource_catalog_invalid',422)
    return copy.deepcopy(value)


def select_entry(catalog,selector):
    validate_schema('ResourceSelector',selector)
    return entry_for_operation(catalog,selector['target_id'],selector['operation'])


def entry_for_operation(catalog,target_id,operation):
    catalog=validate_catalog(catalog)
    need(operation in OPERATIONS,'resource_scope_denied',403)
    matches=[row for row in catalog['entries'] if row['target_id']==target_id]
    need(len(matches)==1,'resource_scope_denied',403)
    entry=matches[0]
    need(entry['kind']==OPERATIONS[operation][0],'resource_scope_denied',403)
    return entry


def profile(entry,operation):
    need(operation in OPERATIONS and entry['kind']==OPERATIONS[operation][0],'resource_scope_denied',403)
    return copy.deepcopy(entry[OPERATIONS[operation][1]])


class InstalledCatalog:
    def __init__(self,path):
        self.path=str(Path(path).absolute())
        self.value=self._read()

    def _read(self):
        value=decode(installed_public_bytes(self.path))
        validate_schema('InstalledCatalog',value)
        entries=value['entries']
        need(len({row['target_id'] for row in entries})==len(entries),'resource_catalog_invalid',422)
        need(len({row['kind'] for row in entries})==len(entries),'resource_catalog_invalid',422)
        for row in entries:
            if row['kind']=='model-peer-health':
                need(row['origin']=='https://192.168.10.55:8021','resource_catalog_invalid',422)
                raw=installed_public_bytes(row['ca_file'],limit=65536)
                need(hashlib.sha256(raw).hexdigest()==row['ca_sha256'],'resource_catalog_invalid',422)
        return value

    def current(self):
        need(self._read()==self.value,'resource_catalog_changed')
        return copy.deepcopy(self.value)

    def resolve(self,target_id,revision):
        rows=[row for row in self.current()['entries'] if row['target_id']==target_id]
        need(len(rows)==1)
        row=rows[0]
        actual='sha256:'+hashlib.sha256(canonical(row)).hexdigest()
        need(actual==revision,'resource_catalog_changed')
        return copy.deepcopy(row)

    def describe(self):
        return [{'kind':row['kind'],'target_id':row['target_id'],
            'target_revision':'sha256:'+hashlib.sha256(canonical(row)).hexdigest()}
            for row in self.current()['entries']]


def validate_policy(value,catalog):
    validate_schema('GatewayPolicy',value)
    checked=copy.deepcopy(value);checked['catalog']=validate_catalog(value['catalog'])
    for entry in checked['catalog']['entries']:
        target=catalog.resolve(entry['target_id'],entry['target_revision'])
        need(target['kind']==entry['kind'],'resource_catalog_invalid',422)
    return checked
