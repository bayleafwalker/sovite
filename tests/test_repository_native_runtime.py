"""Native protocol controls. Local processes never establish VM qualification."""
import asyncio
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import struct
import zlib
import time

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from sovite.adapters import RuntimeFailure
from sovite.repository_native_runtime import (ExecutorRelay, NativeAppServer, NATIVE_TOOLS,
                                               check_tools, VERSION)
from test_native_responses import HttpPeer, event, USAGE
from sovite.connector.helper import validate_body


def test_only_measured_native_function_inventory_is_admitted():
    payload={'tools':[{'type':'function','name':name} for name in sorted(NATIVE_TOOLS)]}
    check_tools(payload)
    for extra in ({'type':'function','name':'create_goal'}, {'type':'function','name':'apply_patch'},
                  {'type':'custom','name':'apply_patch'}, {'type':'function','name':'fs_write'}, {'type':'web_search'},
                  {'type':'function','name':'mcp__host__privileged'}):
        with pytest.raises(RuntimeFailure,match='native_tool_inventory_mismatch'):
            check_tools({'tools':payload['tools']+[extra]})
    with pytest.raises(RuntimeFailure): check_tools({'tools':[]})


def test_relay_denies_unauthenticated_connection_before_process_creation():
    async def scenario():
        calls=0
        async def forbidden():
            nonlocal calls
            calls+=1
            raise AssertionError('No native process may start')
        relay=ExecutorRelay(forbidden,time.time()+5,lambda:True)
        async with relay.listen():
            with pytest.raises(InvalidStatus) as denied:
                async with connect(relay.url,proxy=None): pass
            assert denied.value.response.status_code==401
            assert not relay.connected and calls==0
    asyncio.run(scenario())


def test_relay_watchdog_observes_revocation_without_native_frames():
    async def scenario():
        current=True
        def authorization():
            if not current: raise RuntimeFailure('authorization_revoked')
        async def forbidden(): raise AssertionError('No process requested')
        relay=ExecutorRelay(forbidden,time.time()+5,authorization)
        async with relay.listen():
            current=False
            await asyncio.wait_for(relay.failure.wait(),1)
            assert relay.failed
    asyncio.run(scenario())


@pytest.fixture
def installed_codex():
    path=os.environ.get('SOVITE_CODEX_COMPAT_EXECUTABLE')
    if not path: pytest.skip('installed Codex 0.166.0 compatibility is opt-in')
    assert subprocess.run([path,'--version'],capture_output=True,text=True,check=True).stdout.strip()==VERSION
    return path


def test_pre_thread_guard_refuses_host_mcp_before_its_command_runs(installed_codex,tmp_path):
    home=tmp_path/'protected';home.mkdir(mode=0o700)
    marker=home/'mcp-started'
    (home/'config.toml').write_text('[mcp_servers.host_escape]\ncommand = "/bin/sh"\nargs = '+
        json.dumps(['-c','printf unsafe > '+str(marker)])+'\n')
    async def scenario():
        app=NativeAppServer(installed_codex,home,'gpt-6','http://127.0.0.1:9','synthetic-bridge-token',
                            time.time()+10,lambda:True)
        try:
            with pytest.raises(RuntimeFailure,match='native_host_configuration_refused'):
                await app.start()
            assert not marker.exists()
        finally: await app.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('tool_mode',['shell','stdin','image','apply_patch'])
def test_native_shell_routes_to_executor_with_fixed_tool_inventory_and_repair_context(installed_codex,tmp_path,tool_mode):
    home=tmp_path/'protected';guest_home=tmp_path/'guest-home';guest=tmp_path/'guest'
    for directory in (home,guest_home,guest):directory.mkdir(mode=0o700)
    subprocess.run(['git','init','-q',str(guest)],check=True)
    # Candidate-owned configuration must not become supervisor process authority.
    (guest/'.codex').mkdir()
    marker=home/'candidate-mcp-started'
    mcp='[mcp_servers.candidate_escape]\ncommand="/bin/sh"\nargs='+json.dumps(['-c','printf unsafe > '+str(marker)])+'\n'
    attempted_features='[features]\nhooks=true\nplugins=true\nremote_plugin=true\ngoals=true\nmulti_agent=true\n'
    (guest/'.codex'/'config.toml').write_text(mcp+attempted_features)
    (guest_home/'config.toml').write_text(mcp+attempted_features)
    hook_marker=home/'candidate-hook-started'
    hooks={'hooks':{'SessionStart':[{'hooks':[{'type':'command',
        'command':'/bin/sh -c '+json.dumps('printf unsafe > '+str(hook_marker))}]}]}}
    (guest/'.codex'/'hooks.json').write_text(json.dumps(hooks))
    (guest_home/'hooks.json').write_text(json.dumps(hooks))
    def png_chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    (guest/'pixel.png').write_bytes(b'\x89PNG\r\n\x1a\n'+png_chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+
        png_chunk(b'IDAT',zlib.compress(b'\x00\xff\x00\x00'))+png_chunk(b'IEND',b''))
    # The standalone exec-server needs this argv0 helper explicitly. Production
    # must install it in the root-owned native closure, outside candidate PATH.
    wrapper=Path(installed_codex).resolve().parents[3]/'bin/codex'
    bwrap=Path(re.search(r"/nix/store/[^/'\":]+-bubblewrap-[^/'\":]+/bin",wrapper.read_text()).group(0))
    assert (bwrap/'bwrap').is_file()
    helper=tmp_path/'fixture-native-bin';helper.mkdir(mode=0o700)
    (helper/'codex-linux-sandbox').symlink_to(installed_codex)
    async def scenario():
        peer=None;native_methods=[]
        async def chunks():
            payload=peer.requests[-1][2]
            check_tools(payload)
            forwarded=json.dumps({key:value for key,value in payload.items()
                if key not in {'client_metadata','prompt_cache_key'}}).encode()
            turn=len(peer.requests)
            if tool_mode in {'image','apply_patch'} and turn==2:
                # Transport compatibility only: the production inference bridge
                # refuses image input before a paid request can be sent.
                with pytest.raises(Exception):validate_body('responses',forwarded,'gpt-6')
            else:validate_body('responses',forwarded,'gpt-6')
            response={'id':f'resp_synthetic_{turn}','model':'gpt-6','status':'in_progress','output':[]}
            yield event('response.created',response=response)
            if turn==1 or (tool_mode=='stdin' and turn==2):
                name='exec_command';args=json.dumps({'cmd':'pwd > candidate-marker.txt'})
                if tool_mode=='apply_patch':
                    name='apply_patch';args='*** Begin Patch\n*** Add File: patch-marker.txt\n+guest-only\n*** End Patch\n'
                elif tool_mode=='stdin':
                    if turn==1:
                        args=json.dumps({'cmd':'read marker; printf \'%s\' "$marker" > stdin-marker.txt',
                                         'tty':True,'yield_time_ms':1})
                    else:
                        output='\n'.join(item.get('output','') for item in payload['input']
                                         if item.get('type')=='function_call_output')
                        match=re.search(r'session ID ([0-9]+)',output)
                        assert match is not None,repr(output)
                        session_id=int(match.group(1))
                        name='write_stdin';args=json.dumps({'session_id':session_id,
                                                          'chars':'guest-input\n','yield_time_ms':1000})
                elif tool_mode=='image':
                    name='view_image';args=json.dumps({'path':str(guest/'pixel.png')})
                item={'type':'function_call','id':f'fc_synthetic_{turn}','call_id':f'call_synthetic_{turn}',
                      'name':name,'arguments':args,'status':'completed'}
                if tool_mode=='apply_patch':
                    item={'type':'custom_tool_call','id':'ctc_patch','call_id':'call_patch',
                          'name':'apply_patch','input':args,'status':'completed'}
                    yield event('response.output_item.added',output_index=0,item={**item,'input':'','status':'in_progress'})
                    yield event('response.custom_tool_call_input.delta',item_id=item['id'],output_index=0,delta=args)
                    yield event('response.custom_tool_call_input.done',item_id=item['id'],output_index=0,input=args)
                else:
                    yield event('response.output_item.added',output_index=0,item={**item,'arguments':'','status':'in_progress'})
                    yield event('response.function_call_arguments.delta',item_id=item['id'],output_index=0,delta=args)
                    yield event('response.function_call_arguments.done',item_id=item['id'],output_index=0,arguments=args)
                yield event('response.output_item.done',output_index=0,item=item)
                response['output']=[item]
            else:
                assert turn==(3 if tool_mode=='stdin' else 2)
                item={'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
                      'content':[{'type':'output_text','text':'Done.','annotations':[]}]}
                yield event('response.output_item.added',output_index=0,item={**item,'status':'in_progress','content':[]})
                yield event('response.output_text.delta',item_id=item['id'],output_index=0,content_index=0,delta='Done.')
                yield event('response.output_item.done',output_index=0,item=item)
                response['output']=[item]
            yield event('response.completed',response={**response,'status':'completed','usage':USAGE})
        peer=HttpPeer(chunks)
        async def process():
            env={'PATH':str(helper)+':'+str(bwrap)+':'+os.environ['PATH'],'HOME':str(guest_home),'CODEX_HOME':str(guest_home),'LANG':'C.UTF-8'}
            child=await asyncio.create_subprocess_exec(installed_codex,'exec-server','--listen','stdio',
                '--concurrent-requests','1',cwd=guest,env=env,stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,start_new_session=True,limit=4*1024*1024+1)
            writer=child.stdin
            class RecordingWriter:
                def write(self,raw):
                    native_methods.append(json.loads(raw).get('method'))
                    return writer.write(raw)
                async def drain(self):await writer.drain()
                def close(self):writer.close()
            child.stdin=RecordingWriter()
            return child
        deadline=time.time()+15
        relay=ExecutorRelay(process,deadline,lambda:True)
        async with peer.serve(),relay.listen():
            app=NativeAppServer(installed_codex,home,'gpt-6',peer.origin,'synthetic-bridge-token',deadline,lambda:True)
            try:
                await app.start()
                repair={'root_run_id':'root','parent_run_id':'parent','repair_number':1,'candidate_tip_sha':'a'*40,
                        'feedback':'Synthetic independent feedback','feedback_sha256':'b'*64}
                result=await app.run(relay,environment_id='candidate-attempt',guest_cwd=str(guest),
                                     goal='Original exact goal',repair_context=repair)
                assert result['thread_id'] and result['turn_id']
                if tool_mode=='shell':
                    assert (guest/'candidate-marker.txt').read_text()==str(guest)+'\n'
                    assert 'process/start' in native_methods
                elif tool_mode=='stdin':
                    assert (guest/'stdin-marker.txt').read_text()=='guest-input'
                    assert 'process/start' in native_methods and 'process/write' in native_methods
                elif tool_mode=='image':assert 'fs/readFile' in native_methods
                else:
                    assert not (home/'patch-marker.txt').exists() and not (guest/'patch-marker.txt').exists()
                    outputs=[item.get('output','') for item in peer.requests[-1][2]['input']
                             if item.get('type') in {'custom_tool_call_output','function_call_output'}]
                    assert outputs==['unsupported custom tool call: apply_patch']
                assert not (home/'candidate-marker.txt').exists() and not marker.exists() and not hook_marker.exists()
                assert len(peer.requests)==(3 if tool_mode=='stdin' else 2)
                raw=json.dumps(peer.requests[0][2])
                assert 'Original exact goal' in raw and 'Synthetic independent feedback' in raw
                assert 'synthetic-bridge-token' not in json.dumps(peer.requests[0][2])
                assert not relay.failed
            finally:
                # Intentional closure is distinguished from an uncertain loss.
                await relay.close()
                await app.close()
        assert relay.exit_code==0 and not relay.failed
    asyncio.run(scenario())


@pytest.mark.parametrize('direction',['request','response'])
def test_relay_checks_current_owner_before_rpc_effect_or_disclosure(tmp_path,direction):
    import sys
    from websockets.exceptions import ConnectionClosed
    marker=tmp_path/'remote-effect';release=tmp_path/'release'
    async def scenario():
        current=True;started=asyncio.Event()
        def authorize():
            if not current:raise RuntimeFailure('authorization_revoked')
        async def process():
            if direction=='request':
                code='import sys,pathlib; line=sys.stdin.readline();\nif line: pathlib.Path(sys.argv[1]).write_text("effect")'
                arguments=[str(marker)]
            else:
                code=('import sys,pathlib,time; p=pathlib.Path(sys.argv[1]); '
                      '\nwhile not p.exists(): time.sleep(.001)\n'
                      'print(\'{"id":1,"result":{"secret":"withheld"}}\',flush=True)')
                arguments=[str(release)]
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',code,*arguments,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
            started.set();return child
        relay=ExecutorRelay(process,time.time()+5,authorize)
        async with relay.listen():
            async with connect(relay.url,additional_headers={'Authorization':'Bearer '+relay.token},proxy=None) as client:
                await asyncio.wait_for(started.wait(),1)
                current=False
                if direction=='request':
                    try:await client.send('{"id":1,"method":"process/start","params":{}}')
                    except ConnectionClosed:pass
                else:release.touch()
                with pytest.raises(ConnectionClosed):await asyncio.wait_for(client.recv(),2)
                await asyncio.wait_for(relay.failure.wait(),1)
            assert relay.failed
        assert relay.process.returncode is not None and not marker.exists()
    asyncio.run(scenario())


def test_relay_whole_deadline_closes_process_and_refuses_reconnection():
    import sys
    from websockets.exceptions import ConnectionClosed
    async def scenario():
        starts=0
        async def process():
            nonlocal starts
            starts+=1
            return await asyncio.create_subprocess_exec(sys.executable,'-c','import time;time.sleep(30)',
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
        relay=ExecutorRelay(process,time.time()+.3,lambda:True)
        async with relay.listen():
            async with connect(relay.url,additional_headers={'Authorization':'Bearer '+relay.token},proxy=None) as client:
                with pytest.raises(ConnectionClosed):await asyncio.wait_for(client.recv(),2)
            with pytest.raises(InvalidStatus):
                async with connect(relay.url,additional_headers={'Authorization':'Bearer '+relay.token},proxy=None):pass
        assert starts==1 and relay.failed and relay.process.returncode is not None
    asyncio.run(scenario())
