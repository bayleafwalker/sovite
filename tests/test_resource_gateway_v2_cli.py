"""Typed operator-only policy installation and privilege-drop ordering."""
import copy
from types import SimpleNamespace
import sys

import pytest

from sovite import resource_gateway_v2_cli as cli
from sovite.resource_gateway_store import ResourceOperationStore,canonical
from sovite.resource_target_catalog import InstalledCatalog,ResourceTargetError

pytestmark=pytest.mark.skipif(sys.version_info<(3,12),reason='Released owner contract runtime requires Python3.12')


def setup(tmp_path):
    target={'kind':'kvm-lab','target_id':'public-synthetic-lab','template_id':'diskless-kvm-lab-v1',
            'template_sha256':'f'*64,'max_ttl_seconds':60}
    path=tmp_path/'catalog';path.write_bytes(canonical({'schema_version':'sovite-resource-installed-catalog/v2',
                                                     'entries':[target]}));path.chmod(0o600)
    catalog=InstalledCatalog(path)
    profile={'source_class':'C0','output_class':'C0','destination':'public-fixture'}
    policy={'schema_version':'sovite-resource-policy/v2','gateway_id':'gateway','audience':'resource-gateway',
        'tenant_id':'tenant','principal_id':'operator','workspace_id':'workspace','domain_id':'lab',
        'expires_at':2000000000,'release_policy_revision':'sha256:'+'a'*64,
        'catalog':{'schema_version':'sovite-resource-catalog/v2','entries':[{
            **catalog.describe()[0],'create_profile':profile,'observe_profile':profile,'stop_profile':profile}]}}
    private=tmp_path/'private';private.mkdir(mode=0o700)
    store=ResourceOperationStore(private/'store');store.install()
    return catalog,policy,store


def test_unprivileged_caller_cannot_install_operator_policy(tmp_path,monkeypatch):
    catalog,policy,store=setup(tmp_path)
    with monkeypatch.context() as scoped:
        scoped.setattr(cli.os,'geteuid',lambda:12345)
        with pytest.raises(ResourceTargetError) as error:cli.install_policy(store.path,catalog,canonical(policy))
    assert error.value.code=='resource_operator_required' and store.policy() is None


def test_root_validates_closed_exact_catalog_before_fork(tmp_path,monkeypatch):
    catalog,policy,store=setup(tmp_path)
    monkeypatch.setattr(catalog,'current',lambda:copy.deepcopy(catalog.value))
    original_uid=cli.os.geteuid
    monkeypatch.setattr(cli.os,'geteuid',lambda:0)
    def no_fork():raise AssertionError('Invalid policy reached privilege boundary')
    monkeypatch.setattr(cli.os,'fork',no_fork)
    for mutate in [lambda p:p.update(command='anything'),
                   lambda p:p['catalog']['entries'][0].update(target_revision='sha256:'+'b'*64),
                   lambda p:p['catalog']['entries'][0]['create_profile'].update(source_class='C1')]:
        bad=copy.deepcopy(policy);mutate(bad)
        with pytest.raises(Exception):cli.install_policy(store.path,catalog,canonical(bad))
    with pytest.raises(ResourceTargetError):cli.install_policy(store.path,catalog,b'x'*65537)
    monkeypatch.setattr(cli.os,'geteuid',original_uid)
    assert store.policy() is None


def test_privilege_drop_clears_environment_and_groups_before_store_mutation(tmp_path,monkeypatch):
    catalog,policy,store=setup(tmp_path);identity=cli._database_identity(store.path)
    events=[];environment={'UNRELATED_ROOT_TOKEN':'fixture-secret','PATH':'fixture'}
    monkeypatch.setattr(cli.os,'environ',environment)
    monkeypatch.setattr(cli.os,'umask',lambda _:None)
    monkeypatch.setattr(cli.os,'setgroups',lambda groups:events.append(('groups',groups,dict(environment))))
    monkeypatch.setattr(cli.os,'setgid',lambda gid:events.append(('gid',gid)))
    monkeypatch.setattr(cli.os,'setuid',lambda uid:events.append(('uid',uid)))
    original=ResourceOperationStore.set_policy
    def write(self,value):
        events.append(('write',dict(environment)));original(self,value)
    monkeypatch.setattr(ResourceOperationStore,'set_policy',write)
    cli._write_as_database_owner(store.path,policy,identity,catalog.path)
    assert events==[('groups',[],{}),('gid',identity.st_gid),('uid',identity.st_uid),('write',{})]
    assert store.policy()==policy


def test_database_symlink_and_inode_substitution_refuse_policy_write(tmp_path,monkeypatch):
    catalog,policy,store=setup(tmp_path)
    link=tmp_path/'link';link.symlink_to(store.path)
    with pytest.raises(ResourceTargetError):cli._database_identity(link)
    identity=cli._database_identity(store.path)
    monkeypatch.setattr(cli.os,'environ',{})
    monkeypatch.setattr(cli.os,'umask',lambda _:None)
    monkeypatch.setattr(cli.os,'setgroups',lambda _:None)
    monkeypatch.setattr(cli.os,'setgid',lambda _:None)
    monkeypatch.setattr(cli.os,'setuid',lambda _:None)
    from pathlib import Path
    Path(store.path).rename(store.path+'.old');Path(store.path).write_bytes(b'foreign')
    with pytest.raises(ResourceTargetError):cli._write_as_database_owner(store.path,policy,identity,catalog.path)
