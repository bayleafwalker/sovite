"""Fail-closed supervisor installation and fresh guarded transport tests."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from sovite.adapters import RuntimeFailure
import sovite.repository_native_isolation as isolation
from sovite.repository_native_runtime import NativeAppServer


def test_absent_or_writable_profile_never_starts_raw_native(tmp_path,monkeypatch):
    profile=tmp_path/'profile.json'
    monkeypatch.setattr(isolation,'_PROFILE',profile)
    for exists in (False,True):
        if exists:profile.write_text('{}')
        home=tmp_path/('home-'+str(exists));home.mkdir(mode=0o700)
        async def scenario():
            app=NativeAppServer('/definitely-not-a-native-fallback',home,'gpt-6','http://127.0.0.1:9',
                                'synthetic',time.time()+2,lambda:True)
            try:
                with pytest.raises(RuntimeFailure,match='native_supervisor_unavailable'):await app.start()
                assert app.process is None
            finally:await app.close()
        asyncio.run(scenario())


@pytest.mark.parametrize('origin',['https://127.0.0.1:443','http://localhost:9','http://127.0.0.1:9/path',
    'http://user:password@127.0.0.1:9','http://127.0.0.1:9?target=elsewhere'])
def test_transport_has_no_alternate_endpoint_surface(origin):
    with pytest.raises(RuntimeFailure):isolation.endpoint(origin,'http')


@pytest.mark.parametrize('revoke',['before-connect','before-disclosure'])
def test_fixed_proxy_denies_new_effect_or_post_revocation_disclosure(tmp_path,monkeypatch,revoke):
    async def scenario():
        current=True;effects=[];ready=asyncio.Event();send=asyncio.Event()
        async def target(reader,writer):
            try:
                effects.append('connected');await reader.readexactly(1)
                ready.set();await send.wait()
                writer.write(b'public-synthetic-protected-output');await writer.drain()
            finally:writer.close();await writer.wait_closed()
        server=await asyncio.start_server(target,'127.0.0.1',0)
        profile=SimpleNamespace(sha256='synthetic-unqualified-transport-fixture')
        monkeypatch.setattr(isolation,'load_profile',lambda:profile)
        def authorize():
            if not current:raise RuntimeFailure('repository_currentness_revoked')
        proxy=isolation.Namespace(tmp_path,'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]),
                                  time.time()+3,authorize)
        path=tmp_path/'model.sock'
        listener=await asyncio.start_unix_server(lambda r,w:proxy._accept(r,w,'model'),path=path)
        try:
            if revoke=='before-connect':current=False
            reader,writer=await asyncio.open_unix_connection(path)
            writer.write(b'x');await writer.drain()
            if revoke=='before-disclosure':
                await asyncio.wait_for(ready.wait(),1);current=False;send.set()
            try:received=await asyncio.wait_for(reader.read(),1)
            except ConnectionResetError:received=b''
            assert received==b''
            assert proxy.failed
            assert effects==([]if revoke=='before-connect'else['connected'])
            writer.close()
            try:await writer.wait_closed()
            except ConnectionResetError:pass
        finally:
            send.set();listener.close();server.close()
            await proxy.close();await listener.wait_closed();await server.wait_closed()
    asyncio.run(scenario())


def test_namespace_launch_failure_has_no_raw_fallback(tmp_path,monkeypatch):
    async def scenario():
        profile=SimpleNamespace(sha256='unqualified-launch-fixture',bubblewrap='/fixed/bwrap',
            python='/fixed/python',tools='/fixed/tools',store_paths=(),child='/fixed/child')
        monkeypatch.setattr(isolation,'load_profile',lambda:profile)
        calls=[]
        async def denied(*args,**kwargs):
            calls.append((args,kwargs))
            raise PermissionError('Synthetic container namespace refusal')
        monkeypatch.setattr(asyncio,'create_subprocess_exec',denied)
        namespace=isolation.Namespace(tmp_path,'http://127.0.0.1:9',time.time()+2,lambda:True)
        with pytest.raises(RuntimeFailure,match='native_supervisor_unavailable'):
            await namespace.start(['/fixed/native','app-server'],{'HOME':str(tmp_path)})
        assert len(calls)==1 and calls[0][0][0]=='/fixed/bwrap'
        assert '--unshare-all'in calls[0][0] and '--disable-userns'in calls[0][0]
        assert namespace.root is None and not namespace.servers
    asyncio.run(scenario())
