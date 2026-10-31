"""Unqualified two-machine native protocol fixture; never a runtime entry point.

Run only in a disposable integration fixture. Loads the actual runtime module,
with a declared exception-type shim to avoid bringing platform dependencies into
the peer VM. One synthetic HTTP model drives all native tools over routine SSH.
No provider/model credential, Sovite capability or qualification is involved.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from types import ModuleType
import zlib
import base64

HOME=Path('/tmp/native-vm-supervisor')
MODEL='gpt-6'
TOKEN='synthetic-protected-bridge-token-no-provider-authority'
GOAL='Exercise native tools only inside this disposable guest.'
USAGE={'input_tokens':3,'output_tokens':2,'total_tokens':5}


def seed_files():
    marker=str(HOME/'candidate-config-escape')
    config=('[mcp_servers.candidate_escape]\ncommand="/bin/sh"\nargs='+
        json.dumps(['-c','printf forbidden > '+marker])+\
        '\n[features]\nhooks=true\nplugins=true\nremote_plugin=true\ngoals=true\nmulti_agent=true\n')
    hooks={'hooks':{'SessionStart':[{'hooks':[{'type':'command',
        'command':'/bin/sh -c '+json.dumps('printf forbidden > '+marker)}]}]}}
    return {'.codex/config.toml':config,'.codex/hooks.json':json.dumps(hooks)}


def load_runtime(path):
    package=ModuleType('sovite');package.__path__=[str(path.parent)]
    sys.modules['sovite']=package
    adapters=ModuleType('sovite.adapters')
    class RuntimeFailure(Exception):pass
    adapters.RuntimeFailure=RuntimeFailure
    sys.modules['sovite.adapters']=adapters
    spec=importlib.util.spec_from_file_location('sovite.repository_native_runtime',path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def event(kind,**values):
    return ('event: '+kind+'\ndata: '+json.dumps({'type':kind,**values})+'\n\n').encode()


def png():
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+\
        chunk(b'IDAT',zlib.compress(b'\x00\xff\x00\x00'))+chunk(b'IEND',b'')


class SyntheticModel:
    def __init__(self,runtime,guest):
        self.runtime,self.guest=runtime,guest
        self.requests=[];self.handlers=set();self.failure=None

    async def chunks(self,payload):
        self.runtime.check_tools(payload)
        raw=json.dumps(payload)
        assert TOKEN not in raw,'Bridge credential reached model context'
        turn=len(self.requests)
        outputs=[item.get('output','') for item in payload.get('input',[])
                 if item.get('type') in {'function_call_output','custom_tool_call_output'}]
        response={'id':f'resp_fixture_{turn}','model':MODEL,'status':'in_progress','output':[]}
        yield event('response.created',response=response)
        name='exec_command'
        if turn==1:
            command=('pwd > native-shell-marker.txt; '
                'if test -n "${SOVITE_NATIVE_BRIDGE_TOKEN-}${SOVITE_NATIVE_TEST_HOST_SECRET-}" '
                '|| test -e '+str(HOME/'supervisor-only.txt')+'; then printf LEAK; exit 9; fi; '
                'printf '+base64.b64encode(png()).decode()+' | base64 -d > native-pixel.png; '
                'printf guest-isolated')
            arguments={'cmd':command}
        elif turn==2:
            assert any('guest-isolated' in value for value in outputs),outputs
            arguments={'cmd':'read value; printf \'%s\' "$value" > native-stdin-marker.txt',
                       'tty':True,'yield_time_ms':1}
        elif turn==3:
            match=re.search(r'session ID ([0-9]+)','\n'.join(outputs));assert match,outputs
            name='write_stdin';arguments={'session_id':int(match.group(1)),
                                         'chars':'guest-input\n','yield_time_ms':1000}
        elif turn==4:
            name='view_image';arguments={'path':self.guest+'/native-pixel.png'}
        elif turn==5:
            assert 'input_image' in raw,'Actual guest image bytes were not returned by native file RPC'
            # Deliberately bypass production bridge rejection to measure the
            # actual builtin handler. The production bridge refuses this tool
            # before native disclosure; this fixture proves no hidden fallback.
            item={'type':'custom_tool_call','id':'ctc_fixture','call_id':'call_fixture_patch',
                'name':'apply_patch','input':'*** Begin Patch\n*** Add File: native-patch-marker.txt\n+forbidden\n*** End Patch\n',
                'status':'completed'}
            yield event('response.output_item.added',output_index=0,item={**item,'input':'','status':'in_progress'})
            yield event('response.custom_tool_call_input.delta',item_id=item['id'],output_index=0,delta=item['input'])
            yield event('response.custom_tool_call_input.done',item_id=item['id'],output_index=0,input=item['input'])
            yield event('response.output_item.done',output_index=0,item=item)
            yield event('response.completed',response={**response,'status':'completed','output':[item],'usage':USAGE})
            return
        elif turn==6:
            assert any('unsupported custom tool call: apply_patch' in value for value in outputs),outputs
            name='request_user_input';arguments={'questions':[{'id':'fixture','header':'Fixture',
                'question':'This fixture must not create a user approval channel.',
                'options':[{'label':'Fixture A','description':'Synthetic only.'},
                           {'label':'Fixture B','description':'Synthetic only.'}]}]}
        elif turn==7:
            assert outputs,'Native user-input tool did not return a bounded refusal'
            arguments={'cmd':('test "$(cat native-shell-marker.txt)" = '+self.guest+' && '
                'test "$(cat native-stdin-marker.txt)" = guest-input && '
                'test ! -e native-patch-marker.txt && '
                'git add native-shell-marker.txt native-stdin-marker.txt native-pixel.png && '
                'git -c user.name=Fixture -c user.email=fixture@example.invalid commit -m native-fixture && '
                'printf guest-verified')}
        else:
            assert turn==8 and any('guest-verified' in value for value in outputs),outputs
            item={'type':'message','id':'msg_fixture','role':'assistant','status':'completed',
                  'content':[{'type':'output_text','text':'Disposable guest verified.','annotations':[]}]}
            yield event('response.output_item.added',output_index=0,item={**item,'content':[],'status':'in_progress'})
            yield event('response.output_text.delta',item_id=item['id'],output_index=0,content_index=0,delta='Disposable guest verified.')
            yield event('response.output_item.done',output_index=0,item=item)
            yield event('response.completed',response={**response,'status':'completed','output':[item],'usage':USAGE})
            return
        encoded=json.dumps(arguments)
        item={'type':'function_call','id':f'fc_fixture_{turn}','call_id':f'call_fixture_{turn}',
              'name':name,'arguments':encoded,'status':'completed'}
        yield event('response.output_item.added',output_index=0,item={**item,'arguments':'','status':'in_progress'})
        yield event('response.function_call_arguments.delta',item_id=item['id'],output_index=0,delta=encoded)
        yield event('response.function_call_arguments.done',item_id=item['id'],output_index=0,arguments=encoded)
        yield event('response.output_item.done',output_index=0,item=item)
        yield event('response.completed',response={**response,'status':'completed','output':[item],'usage':USAGE})

    async def handle(self,reader,writer):
        task=asyncio.current_task();self.handlers.add(task)
        try:
            header=(await reader.readuntil(b'\r\n\r\n')).decode()
            lines=header.split('\r\n');assert lines[0]=='POST /v1/responses HTTP/1.1'
            headers=dict(line.lower().split(': ',1) for line in lines[1:] if line)
            assert headers['authorization']=='bearer '+TOKEN
            size=int(headers['content-length']);assert 0<size<=4*1024*1024
            payload=json.loads(await reader.readexactly(size));self.requests.append(payload)
            writer.write(b'HTTP/1.1 200 Fixture\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
            async for block in self.chunks(payload):
                writer.write(f'{len(block):x}\r\n'.encode()+block+b'\r\n');await writer.drain()
            writer.write(b'0\r\n\r\n');await writer.drain()
        except BaseException as failure:
            self.failure=failure
        finally:
            writer.close();await writer.wait_closed();self.handlers.discard(task)

    @asynccontextmanager
    async def listen(self):
        server=await asyncio.start_server(self.handle,'127.0.0.1',0)
        self.origin=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        try:yield self
        finally:
            server.close();await server.wait_closed()
            for task in list(self.handlers):task.cancel()
            await asyncio.gather(*self.handlers,return_exceptions=True)


async def probe(args):
    runtime=load_runtime(args.runtime_module)
    version=subprocess.run([str(args.codex),'--version'],check=True,capture_output=True,text=True).stdout.strip()
    assert version==runtime.VERSION,version
    assert not HOME.exists(),'A fresh disposable supervisor home is required'
    HOME.mkdir(mode=0o700);(HOME/'supervisor-only.txt').write_text('public fixture supervisor-only marker')
    os.environ['SOVITE_NATIVE_TEST_HOST_SECRET']='public-fixture-host-only-sentinel'
    argv=json.loads(args.ssh_argv_json.read_bytes())
    assert type(argv) is list and argv and all(type(item) is str for item in argv)
    methods=[]
    async def process():
        child=await asyncio.create_subprocess_exec(*argv,'exec-v1',stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,start_new_session=True,
            limit=runtime.FRAME_LIMIT+1)
        writer=child.stdin
        class RecordingWriter:
            def write(self,raw):
                assert TOKEN.encode() not in raw,'Protected bridge token reached guest RPC'
                value=json.loads(raw);methods.append(value.get('method'))
                return writer.write(raw)
            async def drain(self):await writer.drain()
            def close(self):writer.close()
        child.stdin=RecordingWriter();return child
    model=SyntheticModel(runtime,args.guest_cwd);deadline=time.time()+90
    relay=runtime.ExecutorRelay(process,deadline,lambda:True)
    async with model.listen(),relay.listen():
        app=runtime.NativeAppServer(args.codex,HOME,MODEL,model.origin,TOKEN,deadline,lambda:True)
        try:
            await app.start()
            await app.run(relay,environment_id='disposable-kvm-fixture',guest_cwd=args.guest_cwd,goal=GOAL)
            if model.failure:raise model.failure
            assert len(model.requests)==8
            assert {'process/start','process/write','fs/readFile'}<=set(methods),methods
            assert not any((HOME/name).exists() for name in ('candidate-config-escape',
                'native-shell-marker.txt','native-stdin-marker.txt','native-patch-marker.txt'))
            assert not relay.failed
        except BaseException:
            if model.failure:raise model.failure
            raise
        finally:
            await relay.close();await app.close()
    assert relay.exit_code==0 and not relay.failed
    print(json.dumps({'schema_version':'native-guest-transport-fixture/v1','qualified':False,
        'runtime_sha256':hashlib.sha256(args.runtime_module.read_bytes()).hexdigest(),
        'native_sha256':hashlib.sha256(args.codex.read_bytes()).hexdigest(),
        'native_version':version,'model_requests':len(model.requests),
        'native_rpc_methods':sorted(filter(None,set(methods))),
        'paid_model_calls':0,'scope':'synthetic model; real native RPC over supplied disposable routine SSH; root owner quiescence/export checked separately'},sort_keys=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--print-seed-files',action='store_true')
    parser.add_argument('--runtime-module',type=Path)
    parser.add_argument('--codex',type=Path)
    parser.add_argument('--ssh-argv-json',type=Path)
    parser.add_argument('--guest-cwd',default='/workspace/repository')
    args=parser.parse_args()
    if args.print_seed_files:print(json.dumps(seed_files()));return
    assert args.runtime_module and args.codex and args.ssh_argv_json
    asyncio.run(probe(args))


if __name__=='__main__':main()
