"""Actual HTTP stream controls; synthetic responses never qualify a model helper."""
import asyncio
from contextlib import asynccontextmanager
import json
import time

import pytest

from sovite.adapters import RuntimeFailure
from sovite.connector.helper import validate_body
from sovite.connector.canonical import ProtocolError
from sovite.model_gateway_client import ModelGatewayInference

MODEL = {"id": "fixed-binding", "adapter": "openai-compatible", "wire_api": "responses",
         "model": "fixed-model"}
PAYLOAD = {"model": "fixed-model", "input": [{"role": "user", "content": "synthetic"}],
           "stream": True, "store": False, "include": ["reasoning.encrypted_content"],
           "reasoning": {"summary": "auto"}, "tools": [], "tool_choice": "auto"}
USAGE = {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def event(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


def completed(*, usage=USAGE, model="fixed-model", output=None):
    return event("response.completed", response={"id": "resp_synthetic", "status": "completed",
                 "model": model, "output": output or [], "usage": usage})


class HttpPeer:
    def __init__(self, chunks, *, cut=False, status=200, content_type="text/event-stream"):
        self.chunks, self.cut, self.status, self.content_type = chunks, cut, status, content_type
        self.requests = []; self.handlers = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task(); self.handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            headers = dict(line.lower().split(": ", 1) for line in lines[1:] if line)
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            self.requests.append((lines[0], headers, json.loads(body)))
            writer.write((f"HTTP/1.1 {self.status} Synthetic\r\nContent-Type: {self.content_type}\r\n"
                          "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode())
            await writer.drain()
            async for block in self.chunks():
                writer.write(f"{len(block):x}\r\n".encode() + block + b"\r\n")
                await writer.drain()
            if not self.cut:
                writer.write(b"0\r\n\r\n"); await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except ConnectionError: pass
            self.handlers.discard(task)

    @asynccontextmanager
    async def serve(self):
        server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        try:
            yield self
        finally:
            server.close(); await server.wait_closed()
            tasks = list(self.handlers)
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def client(self, *, deadline=None):
        # Deliberate synthetic loopback transport. Production from_environment
        # requires HTTPS and the actual started owner capability.
        return ModelGatewayInference(origin=self.origin, token='synthetic-workload-token', verify=True,
            binding_id=MODEL['id'], model_id=MODEL['model'], generation='sha256:'+'a'*64,
            job_id='synthetic-job', fence='b'*32, deadline=deadline or time.time()+10,
            capability='synthetic-one-job-capability', workload='synthetic-worker', binding_timeout=5)


def blocks(*values):
    async def chunks():
        for value in values:
            yield value
            await asyncio.sleep(.005)
    return chunks


def test_fixed_binding_strips_only_local_metadata_and_exposes_completed_usage():
    async def scenario():
        raw = event('response.created', response={'id':'resp_synthetic'}) + completed()
        peer = HttpPeer(blocks(raw))
        async with peer.serve():
            payload = {**PAYLOAD, 'client_metadata': {'private':'bridge-only'}, 'prompt_cache_key':'local-session'}
            result = await peer.client().responses(MODEL, payload)
            assert result.sse == raw and result.usage == USAGE
            assert result.response['status'] == 'completed'
            request, headers, sent = peer.requests[0]
            assert request == 'POST /inference/fixed-binding/responses HTTP/1.1'
            assert headers['x-sovite-fence'] == 'b'*32
            assert headers['x-sovite-job'] == 'synthetic-job'
            assert headers['x-sovite-capability'] == 'synthetic-one-job-capability'
            assert sent == PAYLOAD and 'client_metadata' in payload
    asyncio.run(scenario())


def test_completed_without_usage_preserves_unknown_usage():
    async def scenario():
        peer = HttpPeer(blocks(completed(usage=None)))
        async with peer.serve():
            result = await peer.client().responses(MODEL, PAYLOAD)
            assert result.usage is None
    asyncio.run(scenario())


@pytest.mark.parametrize('cut', [False, True])
def test_lost_or_missing_terminal_completion_is_uncertain_without_replay(cut):
    async def scenario():
        peer = HttpPeer(blocks(event('response.created', response={'id':'resp_synthetic'})), cut=cut)
        async with peer.serve():
            with pytest.raises(RuntimeFailure, match='external_execution_uncertain') as failure:
                await peer.client().responses(MODEL, PAYLOAD)
            assert failure.value.retryable is False
            assert len(peer.requests) == 1
    asyncio.run(scenario())


def test_whole_deadline_expires_even_while_bytes_keep_arriving():
    async def chunks():
        for _ in range(100):
            yield b': synthetic keepalive\n\n'
            await asyncio.sleep(.015)
    async def scenario():
        peer = HttpPeer(chunks)
        async with peer.serve():
            start = time.monotonic()
            with pytest.raises(RuntimeFailure, match='execution_timeout'):
                await peer.client(deadline=time.time()+.12).responses(MODEL, PAYLOAD)
            assert time.monotonic()-start < .6 and len(peer.requests) == 1
    asyncio.run(scenario())


def test_revocation_after_last_block_prevents_disclosure():
    async def scenario():
        peer = HttpPeer(blocks(completed()))
        checks = 0
        def authority():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise RuntimeFailure('authorization_revoked')
        async with peer.serve():
            with pytest.raises(RuntimeFailure, match='authorization_revoked'):
                await peer.client().responses(MODEL, PAYLOAD, authority)
            assert checks == 3
    asyncio.run(scenario())


def test_revocation_between_blocks_stops_stream_without_completion():
    async def scenario():
        first_seen = asyncio.Event(); revoked = False; checks = 0
        def authority():
            nonlocal checks
            checks += 1
            if revoked: raise RuntimeFailure('authorization_revoked')
            if checks >= 2: first_seen.set()
        async def chunks():
            nonlocal revoked
            yield event('response.created', response={'id':'resp_synthetic'})
            await asyncio.wait_for(first_seen.wait(), 1)
            revoked = True
            yield completed()
        peer = HttpPeer(chunks)
        async with peer.serve():
            with pytest.raises(RuntimeFailure, match='authorization_revoked'):
                await peer.client().responses(MODEL, PAYLOAD, authority)
    asyncio.run(scenario())


@pytest.mark.parametrize('body,code', [
    (b'event: sovite_error\ndata: {"code":"authorization_revoked","message":"synthetic-one-job-capability"}\n\n', 'authorization_revoked'),
    (b'event: sovite_error\ndata: {"code":"deadline_exceeded"}\n\n', 'execution_timeout'),
    (event('response.failed', response={'error': {'message':'synthetic-workload-token'}}), 'upstream_rejected'),
    (completed(model='foreign-model'), 'model_protocol_error'),
    (completed(usage={'input_tokens':3,'output_tokens':2,'total_tokens':0}), 'model_protocol_error'),
    (completed(usage={**USAGE,'input_tokens_details':{'cached_tokens':10}}), 'model_protocol_error'),
    (b'x'*(4*1024*1024+1), 'upstream_response_too_large'),
])
def test_stream_failures_are_bounded_and_never_echo_bearers(body, code):
    async def scenario():
        peer = HttpPeer(blocks(body))
        async with peer.serve():
            with pytest.raises(RuntimeFailure) as failure:
                await peer.client().responses(MODEL, PAYLOAD)
            assert failure.value.code == code
            assert 'synthetic-one-job-capability' not in str(failure.value)
            assert 'synthetic-workload-token' not in str(failure.value)
            assert len(peer.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'tools':[{'type':'web_search'}]}, {'tools':[{'type':'custom','name':'apply_patch'}]},
    {'include':['web_search_call.results']}, {'caller_selected_url':'https://foreign.invalid'},
    {'input':[{'role':'user','content':[{'type':'input_image','image_url':'https://foreign.invalid'}]}]},
    {'input':[{'type':'reasoning','summary':[],'encrypted_content':'opaque','url':'https://foreign.invalid'}]},
    {'input':[{'type':'reasoning','summary':[],'content':'plaintext'}]},
    {'input':[{'type':'reasoning','summary':[],'encrypted_content':'opaque','content':[{'type':'reasoning_text','text':'plaintext'}]}]},
    {'input':[{'type':'compaction','encrypted_content':'opaque'}]},
    {'reasoning':{'unknown':'field'}},
])
def test_unsupported_native_fields_fail_before_http(changes):
    async def scenario():
        peer = HttpPeer(blocks(completed()))
        async with peer.serve():
            with pytest.raises(RuntimeFailure, match='model_protocol_error'):
                await peer.client().responses(MODEL, {**PAYLOAD, **changes})
            assert peer.requests == []
    asyncio.run(scenario())


def test_closed_encrypted_reasoning_and_function_roundtrip():
    reasoning = {'type':'reasoning','id':'rs_synthetic','summary':[{'type':'summary_text','text':'Synthetic summary'}],
                 'encrypted_content':'opaque-synthetic-ciphertext','status':'completed'}
    payload = {**PAYLOAD, 'input':[reasoning,
        {'type':'function_call','id':'fc_synthetic','call_id':'call_synthetic','name':'exec_command','arguments':'{"cmd":"true"}'},
        {'type':'function_call_output','call_id':'call_synthetic','output':'synthetic result'}],
        'tools':[{'type':'function','name':'exec_command','parameters':{'type':'object'}}]}
    assert validate_body('responses', json.dumps(payload).encode(), 'fixed-model')['input'][0] == reasoning
    with pytest.raises(ProtocolError):
        validate_body('responses', json.dumps({**payload,'include':[]}).encode(), 'fixed-model')


@pytest.mark.parametrize('provider',['gateway','opencode-go'])
def test_installed_codex_function_loop_and_encrypted_reasoning_roundtrip(tmp_path,monkeypatch,provider):
    """Opt-in installed binary compatibility, not real-model or VM qualification."""
    import os
    import signal
    import subprocess
    executable = os.environ.get('SOVITE_CODEX_COMPAT_EXECUTABLE')
    if not executable:
        pytest.skip('set SOVITE_CODEX_COMPAT_EXECUTABLE for installed Codex 0.166.0 compatibility')
    version = subprocess.run([executable, '--version'], capture_output=True, text=True, check=True).stdout.strip()
    assert version == 'codex-cli 0.166.0'
    repo = tmp_path/'repo'; repo.mkdir()
    home = tmp_path/'home'; home.mkdir()
    subprocess.run(['git','init','-q',str(repo)], check=True)
    model_name='gpt-6-luna' if provider=='opencode-go' else 'gpt-6'
    native_model = {**MODEL,'model':model_name}
    usage=USAGE
    if provider=='opencode-go':
        from sovite.policy import GO_ENDPOINT
        native_model.update(adapter='opencode-go',endpoint=GO_ENDPOINT,identity_protocol='provider-service',
                            api_key_env='SYNTHETIC_GO_COMPAT_KEY')
        monkeypatch.setenv('SYNTHETIC_GO_COMPAT_KEY','public-synthetic-key-no-provider-authority')
        usage={**USAGE,'cost':0,'opencode':{'billing_source':'go','list_cost':.0000013,'allowance':.0000052}}
    cipher = 'opaque-synthetic-encrypted-reasoning'
    async def scenario():
        peer = None
        async def chunks():
            turn = len(peer.requests)
            response = {'id':f'resp_synthetic_{turn}','object':'response','created_at':int(time.time()),
                        'model':model_name,'status':'in_progress','output':[]}
            yield event('response.created', response=response)
            if turn == 1:
                reasoning = {'type':'reasoning','id':'rs_synthetic','summary':[],
                             'encrypted_content':cipher,'status':'completed'}
                yield event('response.output_item.added', output_index=0, item=reasoning)
                yield event('response.output_item.done', output_index=0, item=reasoning)
                arguments = json.dumps({'cmd':"printf '%s\\n' 'synthetic response tool loop' > result.txt"})
                call = {'type':'function_call','id':'fc_synthetic','call_id':'call_synthetic',
                        'name':'exec_command','arguments':arguments,'status':'completed'}
                yield event('response.output_item.added', output_index=1, item={**call,'arguments':'','status':'in_progress'})
                yield event('response.function_call_arguments.delta', item_id=call['id'], output_index=1, delta=arguments)
                yield event('response.function_call_arguments.done', item_id=call['id'], output_index=1, arguments=arguments)
                yield event('response.output_item.done', output_index=1, item=call)
                response['output'] = [reasoning,call]
            else:
                assert turn == 2, 'synthetic endpoint never services an unbounded native loop'
                text = 'Created the synthetic result.'
                part = {'type':'output_text','text':text,'annotations':[]}
                message = {'id':'msg_synthetic','type':'message','role':'assistant','status':'completed','content':[part]}
                yield event('response.output_item.added', output_index=0, item={**message,'status':'in_progress','content':[]})
                yield event('response.content_part.added', item_id=message['id'], output_index=0, content_index=0,
                            part={**part,'text':''})
                yield event('response.output_text.delta', item_id=message['id'], output_index=0, content_index=0, delta=text)
                yield event('response.output_text.done', item_id=message['id'], output_index=0, content_index=0, text=text)
                yield event('response.content_part.done', item_id=message['id'], output_index=0, content_index=0, part=part)
                yield event('response.output_item.done', output_index=0, item=message)
                response['output'] = [message]
            yield event('response.completed', response={**response,'status':'completed','usage':usage})
        peer = HttpPeer(chunks)
        native_requests = []; bridge_failures = []; handlers = set()
        async with peer.serve():
            if provider=='opencode-go':
                from types import SimpleNamespace
                from sovite.repository_native_inference import DirectResponses
                gateway=DirectResponses(SimpleNamespace(model=native_model,generation='sha256:'+'a'*64,
                    job_id='synthetic-go-compat-job',fence='b'*32,deadline=time.time()+35))
                _,headers=gateway._responses_destination(time.time()+30)
                gateway._responses_destination=lambda deadline:(peer.origin+'/responses',headers)
            else:
                gateway = peer.client(); gateway.model_id = model_name
            async def bridge(reader, writer):
                task = asyncio.current_task(); handlers.add(task)
                try:
                    head = await reader.readuntil(b'\r\n\r\n')
                    headers = dict(line.lower().split(': ',1) for line in head.decode().split('\r\n')[1:] if line)
                    body = await reader.readexactly(int(headers['content-length']))
                    payload = json.loads(body); native_requests.append(payload)
                    try:
                        result = await gateway.responses(native_model, payload)
                        status, raw = 200, result.sse
                    except RuntimeFailure as exc:
                        bridge_failures.append(exc.code)
                        status, raw = 400, json.dumps({'error':{'message':exc.code,'type':'invalid_request_error'}}).encode()
                    writer.write((f'HTTP/1.1 {status} Synthetic\r\nContent-Type: text/event-stream\r\n'
                                  f'Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n').encode()+raw)
                    await writer.drain()
                finally:
                    writer.close()
                    try: await writer.wait_closed()
                    except ConnectionError: pass
                    handlers.discard(task)
            server = await asyncio.start_server(bridge,'127.0.0.1',0)
            configs = {'model_provider':'sovite', 'model_providers.sovite.name':'Fixed synthetic compatibility endpoint',
                'model_providers.sovite.base_url':f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1',
                'model_providers.sovite.wire_api':'responses','model_providers.sovite.requires_openai_auth':False,
                'model_providers.sovite.supports_websockets':False,'model_providers.sovite.request_max_retries':0,
                'model_providers.sovite.stream_max_retries':0,'approval_policy':'never','web_search':'disabled',
                'features.apps':False,'features.hooks':False,'features.memories':False,'features.multi_agent':False,
                'tools.view_image':False,'project_doc_max_bytes':0}
            if provider=='opencode-go':
                from sovite.repository_native_runtime import DISABLED_FEATURES,install_model_catalog
                install_model_catalog(home,model_name)
                configs.update({'features.'+name:False for name in DISABLED_FEATURES})
                configs.update({'agents.enabled':False,'model_catalog_json':str(home/'owner-native-model-catalog.json')})
            command = [executable,'exec','--ignore-user-config','--ignore-rules','--ephemeral','--skip-git-repo-check',
                       '--sandbox','workspace-write','--cd',str(repo),'--model',model_name,'--json']
            for key,value in configs.items(): command += ['-c',key+'='+json.dumps(value)]
            command += ['Create result.txt containing the synthetic response tool loop marker, then finish.']
            env = {'PATH':os.environ['PATH'],'HOME':str(home),'CODEX_HOME':str(home),
                   'NO_PROXY':'127.0.0.1','LANG':'C.UTF-8','TMPDIR':str(tmp_path)}
            process = await asyncio.create_subprocess_exec(*command,env=env,stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE,start_new_session=True)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 35)
            except BaseException:
                if process.returncode is None:
                    os.killpg(process.pid,signal.SIGKILL); await process.wait()
                raise
            finally:
                server.close(); await server.wait_closed()
                tasks = list(handlers)
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
            assert process.returncode == 0, (bridge_failures,stderr.decode()[-2000:],stdout.decode()[-2000:])
            assert (repo/'result.txt').read_text() == 'synthetic response tool loop\n'
            assert len(native_requests) == len(peer.requests) == 2
            replay = native_requests[1]['input']
            assert any(item.get('type')=='reasoning' and item.get('encrypted_content')==cipher for item in replay)
            assert any(item.get('type')=='function_call_output' and item.get('call_id')=='call_synthetic' for item in replay)
            for _,_,forwarded in peer.requests:
                assert 'client_metadata' not in forwarded and 'prompt_cache_key' not in forwarded
                assert all(tool['type']=='function' for tool in forwarded['tools'])
    asyncio.run(scenario())
