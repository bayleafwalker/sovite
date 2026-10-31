"""Synthetic OS-privilege probe for a disposable native supervisor fixture.

This intentionally exercises native host RPCs to measure process privileges;
it does not claim the admitted model tools expose these host RPC methods.
No real DB, S3, provider, delegation or acceptance credential is used.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from native_vm_protocol import load_runtime

SENTINEL='public-synthetic-parent-db-s3-marker'


async def probe(args):
    runtime=load_runtime(args.runtime_module)
    # Declared immutable source-only test inputs. Production always reads the
    # fixed installed profile and fixed adjacent child module, with no setting.
    import sovite.repository_native_isolation as isolation
    if args.fixture_profile:
        isolation._PROFILE=args.fixture_profile
        isolation._CHILD=args.fixture_child
    guest_python=isolation.load_profile().python
    with tempfile.TemporaryDirectory(prefix='native-containment-proof-')as directory:
        root=Path(directory);root.chmod(0o700)
        home=root/'native-home';home.mkdir(mode=0o700)
        config=root/'api-database-and-s3.json';config.write_text(SENTINEL);config.chmod(0o600)
        mirror=root/'private-mirror';mirror.write_text(SENTINEL);mirror.chmod(0o600)
        effects=[]
        async def endpoint(reader,writer):
            try:
                await reader.readuntil(b'\r\n\r\n');effects.append('direct-nonbridge-network')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');await writer.drain()
            finally:writer.close();await writer.wait_closed()
        server=await asyncio.start_server(endpoint,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        app=runtime.NativeAppServer(args.codex,home,'gpt-6','http://127.0.0.1:9',
            'public-synthetic-attempt-bridge-token',time.time()+20,lambda:True)
        try:
            await app.start()
            async def visible(path,needle):
                try:
                    response=await app.request('fs/readFile',{'path':str(path)})
                    return needle in base64.b64decode(response['dataBase64'])
                except runtime.RuntimeFailure:return False
            exposed={name:await visible(path,SENTINEL.encode())for name,path in
                     [('parent_config',config),('mirror',mirror),('parent_proc','/proc/'+str(os.getpid())+'/environ')]}
            exposed['parent_proc_status']=await visible('/proc/'+str(os.getpid())+'/status',('Pid:\t'+str(os.getpid())).encode())
            own=home/'own-home-marker';own.write_text(SENTINEL)
            assert await visible(own,SENTINEL.encode()),'The own-home positive control must work'
            command='import urllib.request; urllib.request.urlopen("http://127.0.0.1:'+str(port)+'",timeout=1)'
            network=await app.request('command/exec',{'command':[guest_python,'-c',command],
                'cwd':str(home),'sandboxPolicy':{'type':'dangerFullAccess'},'timeoutMs':3000,'outputBytesCap':4096})
            daemon=home/'bounded-daemon-marker'
            # Finite self-expiry keeps a damaged fixture bounded too. Private
            # PID namespace teardown must stop it immediately on parent close.
            source=('import time,pathlib; p=pathlib.Path('+repr(str(daemon))+'); '
                'end=time.monotonic()+2; '\
                '\nwhile time.monotonic()<end:\n with p.open("a")as f:f.write("x")\n time.sleep(.01)')
            launch='import subprocess; subprocess.Popen(['+repr(guest_python)+',"-c",'+repr(source)+'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)'
            launched=await app.request('command/exec',{'command':[guest_python,'-c',launch],
                'cwd':str(home),'sandboxPolicy':{'type':'dangerFullAccess'},'timeoutMs':3000,'outputBytesCap':4096})
            assert launched['exitCode']==0
            for _ in range(100):
                if daemon.exists():break
                await asyncio.sleep(.01)
            assert daemon.exists(),'The detached-child positive control must really start'
            await app.close()
            stopped_size=daemon.stat().st_size
            await asyncio.sleep(.1)
            stopped=daemon.stat().st_size==stopped_size
            deadline_home=root/'deadline-native-home';deadline_home.mkdir(mode=0o700)
            deadline_app=runtime.NativeAppServer(args.codex,deadline_home,'gpt-6','http://127.0.0.1:9',
                'public-synthetic-attempt-bridge-token',time.time()+3,lambda:True)
            deadline_marker=deadline_home/'deadline-daemon'
            try:
                await deadline_app.start()
                deadline_source=source.replace(str(daemon),str(deadline_marker)).replace('+2;','+6;')
                # repr(source) appears in the launcher; replace that encoded
                # literal rather than depending on source whitespace escaping.
                deadline_launch=launch.replace(repr(source),repr(deadline_source))
                launched=await deadline_app.request('command/exec',{'command':[guest_python,'-c',deadline_launch],
                    'cwd':str(deadline_home),'sandboxPolicy':{'type':'dangerFullAccess'},'timeoutMs':1000,'outputBytesCap':4096})
                assert launched['exitCode']==0
                for _ in range(100):
                    if deadline_marker.exists():break
                    await asyncio.sleep(.01)
                assert deadline_marker.exists()
                await asyncio.sleep(max(0,deadline_app.deadline-time.time())+.15)
                size=deadline_marker.stat().st_size
                await asyncio.sleep(.1)
                deadline_stopped=deadline_marker.stat().st_size==size
                assert deadline_app.process.returncode is not None
            finally:await deadline_app.close()
            answer={'schema_version':'native-supervisor-containment-probe/v1','qualified':False,
                'native_sha256':hashlib.sha256(args.codex.read_bytes()).hexdigest(),
                'runtime_sha256':hashlib.sha256(args.runtime_module.read_bytes()).hexdigest(),
                'parent_marker_visibility':exposed,'own_home_positive':True,'direct_network_effects':len(effects),
                'direct_network_exit_code':network['exitCode'],'detached_child_stopped':stopped,'deadline_child_stopped':deadline_stopped,
                'parent_uid':os.getuid(),'paid_model_calls':0,
                'scope':'Actual native host-RPC privilege seam in a declared disposable synthetic fixture'}
            print(json.dumps(answer,sort_keys=True),flush=True)
            assert not any(exposed.values())and not effects and network['exitCode']!=0 and stopped and deadline_stopped
        finally:
            await app.close();server.close();await server.wait_closed()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-module',type=Path,required=True)
    parser.add_argument('--codex',type=Path,required=True)
    parser.add_argument('--fixture-profile',type=Path)
    parser.add_argument('--fixture-child',type=Path)
    args=parser.parse_args()
    assert bool(args.fixture_profile)==bool(args.fixture_child)
    if os.environ.get('SOVITE_SYNTHETIC_API_ENV')!=SENTINEL:
        # Never place real parent environment material in this failure probe.
        env={'PATH':os.environ.get('PATH','/run/current-system/sw/bin'),'LANG':'C.UTF-8',
             'SOVITE_SYNTHETIC_API_ENV':SENTINEL}
        os.execve(sys.executable,[sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]],env)
    asyncio.run(asyncio.wait_for(probe(args),25))


if __name__=='__main__':main()
