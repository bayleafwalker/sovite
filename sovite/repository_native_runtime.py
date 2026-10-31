"""Protected Codex loop over its native execution-environment protocol.

The relay changes transport framing only. The VM owns command/file effects;
this module cannot provision a VM, qualify one, or settle model accounting.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
from http import HTTPStatus
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import time
from urllib.parse import urlsplit

from websockets.asyncio.server import serve

from .adapters import RuntimeFailure

VERSION = 'codex-cli 0.166.0'
FRAME_LIMIT = 4 * 1024 * 1024
# These are measured 0.166.0 flags, rather than legacy aliases such as apps.
DISABLED_FEATURES = frozenset({'hooks', 'plugins', 'remote_plugin', 'plugin_discovery_prefetch',
    'skill_mcp_dependency_install', 'goals', 'multi_agent', 'enable_mcp_apps',
    'codex_apps_mcp_2026_07_28', 'mcp_2026_07_28', 'mcp_connection_sharing'})
NATIVE_TOOLS = frozenset({'exec_command', 'write_stdin', 'request_user_input', 'view_image'})


def need(value, code='native_runtime_unavailable'):
    if not value:
        raise RuntimeFailure(code)


def check_tools(payload):
    """The inference bridge calls this before reserving or sending model bytes."""
    tools = payload.get('tools')
    need(type(tools) is list, 'native_tool_inventory_mismatch')
    names = []
    for tool in tools:
        need(type(tool) is dict and tool.get('type') == 'function'
             and type(tool.get('name')) is str, 'native_tool_inventory_mismatch')
        names.append(tool['name'])
    need(set(names) == NATIVE_TOOLS and len(names) == len(NATIVE_TOOLS),
         'native_tool_inventory_mismatch')


def check_response_tools(response, events=()):
    def check_item(item):
        need(type(item) is dict and item.get('type') in {'message','reasoning','function_call'},
             'native_tool_inventory_mismatch')
        if item['type']=='function_call':
            need(item.get('name') in NATIVE_TOOLS,'native_tool_inventory_mismatch')
    need(type(response) is dict and type(response.get('output')) is list,'native_tool_inventory_mismatch')
    for item in response['output']:check_item(item)
    functions=set()
    for event in events:
        kind=event.get('type','')
        need('custom_tool' not in kind,'native_tool_inventory_mismatch')
        if 'item' in event:
            item=event['item'];check_item(item)
            if item['type']=='function_call':functions.add(item.get('id'))
        if kind.startswith('response.function_call_arguments.'):
            need(event.get('item_id') in functions,'native_tool_inventory_mismatch')
        inner=event.get('response')
        if type(inner) is dict:
            for item in inner.get('output',[]):check_item(item)


def executable(path, expected_sha256):
    """Pin a public immutable native executable, never a caller-selected plugin."""
    target = Path(path)
    need(target.is_absolute() and target.resolve(strict=True) == target
         and target.name == 'codex' and len(expected_sha256) == 64)
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0
             and not info.st_mode & 0o022 and info.st_mode & 0o111)
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        need(hmac.compare_digest(digest.hexdigest(), expected_sha256))
    finally:
        os.close(fd)
    return target


async def stop_process(process, *, grace=3):
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), grace)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
    return process.returncode


class ExecutorRelay:
    """One authenticated local connection to one fixed executor process.

    Production supplies the fixed SSH forced-command process factory. Tests
    can use a local exec-server process; that is not a VM isolation claim.
    Reconnection is refused after any connection, including uncertain loss.
    """
    def __init__(self, process_factory, deadline, authorization_check):
        self.process_factory = process_factory
        self.deadline = deadline
        self.authorization_check = authorization_check
        self.token = secrets.token_urlsafe(32)
        self.connection = None
        self.process = None
        self.connected = False
        self.failed = False
        self.exit_code = None
        self._tasks = set()
        self._closing = False
        self.failure = asyncio.Event()
        self._close_lock = asyncio.Lock()

    def check(self):
        self.authorization_check()
        need(time.time() < self.deadline, 'execution_timeout')

    async def _authenticate(self, connection, request):
        raw = request.headers.get('authorization', '').encode('utf-8')
        if self._closing or self.connected or not hmac.compare_digest(raw, ('Bearer ' + self.token).encode('ascii')):
            return connection.respond(HTTPStatus.UNAUTHORIZED, 'Executor unavailable')

    async def _relay(self, connection):
        if self.connected:
            await connection.close(code=1011, reason='Executor unavailable')
            return
        self.connected = True
        self.connection = connection
        task = asyncio.current_task()
        self._tasks.add(task)
        operations = []
        try:
            self.check()
            self.process = await self.process_factory()
            async def incoming():
                async for message in connection:
                    self.check()
                    raw = message.encode('utf-8') if isinstance(message, str) else message
                    need(raw and len(raw) <= FRAME_LIMIT and b'\n' not in raw,
                         'native_executor_protocol_error')
                    # Validate framing without interpreting commands or paths.
                    need(type(json.loads(raw)) is dict, 'native_executor_protocol_error')
                    self.process.stdin.write(raw + b'\n')
                    await self.process.stdin.drain()
            async def outgoing():
                while raw := await self.process.stdout.readline():
                    self.check()
                    need(raw.endswith(b'\n') and len(raw) <= FRAME_LIMIT,
                         'native_executor_protocol_error')
                    need(type(json.loads(raw)) is dict, 'native_executor_protocol_error')
                    await connection.send(raw[:-1].decode('utf-8'))
            operations = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            async with asyncio.timeout(max(0.001, self.deadline-time.time())):
                done, _ = await asyncio.wait(operations, return_when=asyncio.FIRST_COMPLETED)
                for operation in done:
                    operation.result()
        except BaseException:
            if not self._closing:
                self.failed = True
                self.failure.set()
        finally:
            if not self._closing:
                self.failed = True
                self.failure.set()
            for operation in operations:
                operation.cancel()
            await asyncio.gather(*operations, return_exceptions=True)
            if self.process is not None:
                self.exit_code = await stop_process(self.process)
                if self.exit_code:
                    self.failed = True
                    self.failure.set()
            try:
                await connection.close()
            except Exception:
                pass
            self._tasks.discard(task)

    @asynccontextmanager
    async def listen(self):
        self.check()
        async with serve(self._relay, '127.0.0.1', 0, process_request=self._authenticate,
                         max_size=FRAME_LIMIT, compression=None, ping_interval=None) as listener:
            port = listener.sockets[0].getsockname()[1]
            self.url = f'ws://127.0.0.1:{port}'
            async def watch():
                try:
                    while True:
                        await asyncio.sleep(.25)
                        self.check()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failed = True
                    self.failure.set()
                    await self.close()
            watchdog = asyncio.create_task(watch())
            try:
                yield self
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
                await self.close()

    async def close(self):
        async with self._close_lock:
            self._closing = True
            if self.connection is not None:
                await self.connection.close()
            tasks = [task for task in self._tasks if task is not asyncio.current_task()]
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                except TimeoutError:
                    self.failed = True
                    self.failure.set()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            if self.process is not None and self.process.returncode is None:
                self.exit_code = await stop_process(self.process)
            return self.exit_code


class NativeAppServer:
    """One fresh protected native loop. No candidate path is a local cwd."""
    def __init__(self, executable_path, home, model, bridge_origin, bridge_token,
                 deadline, authorization_check):
        self.executable = str(executable_path)
        self.home = Path(home)
        self.model = model
        parsed = urlsplit(bridge_origin)
        need(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
             and parsed.port and parsed.path in {'', '/'}
             and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        self.bridge_origin, self.bridge_token = bridge_origin.rstrip('/'), bridge_token
        self.deadline, self.authorization_check = deadline, authorization_check
        self.process = None
        self.request_id = 0
        self.terminals = {}

    def check(self):
        self.authorization_check()
        need(time.time() < self.deadline, 'execution_timeout')

    def configuration(self):
        config = {'model_provider':'sovite', 'model_providers.sovite.name':'Owner-bound native inference',
            'model_providers.sovite.base_url':self.bridge_origin+'/v1',
            'model_providers.sovite.wire_api':'responses',
            'model_providers.sovite.requires_openai_auth':False,
            'model_providers.sovite.env_key':'SOVITE_NATIVE_BRIDGE_TOKEN',
            'model_providers.sovite.supports_websockets':False,
            'model_providers.sovite.request_max_retries':0,
            'model_providers.sovite.stream_max_retries':0,
            'cli_auth_credentials_store':'file','web_search':'disabled',
            'project_doc_max_bytes':0,'history.persistence':'none'}
        config.update({'features.'+name:False for name in DISABLED_FEATURES})
        return config

    async def start(self):
        self.check()
        info = self.home.stat()
        need(self.home.resolve() == self.home and info.st_uid == os.getuid()
             and not info.st_mode & 0o077)
        command = [self.executable, 'app-server', '--listen', 'stdio://', '--strict-config']
        for key, value in self.configuration().items():
            command += ['-c',key+'='+json.dumps(value)]
        # No inherited provider, forge, workload, database or operator material.
        env = {'PATH':'/run/current-system/sw/bin', 'HOME':str(self.home),
               'CODEX_HOME':str(self.home),'LANG':'C.UTF-8','NO_PROXY':'127.0.0.1',
               'SOVITE_NATIVE_BRIDGE_TOKEN':self.bridge_token}
        self.process = await asyncio.create_subprocess_exec(*command,cwd=self.home,env=env,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,limit=FRAME_LIMIT+1)
        await self.request('initialize',{'clientInfo':{'name':'sovite-native-vm','version':'1'},
            'capabilities':{'experimentalApi':True,'explicitGatewayOauth':True}})
        await self.send({'method':'initialized','params':{}})
        effective = await self.request('config/read',{'cwd':str(self.home),'includeLayers':True})
        config = effective.get('config',{})
        # This guard is before thread/start, where MCP commands can first run.
        need(not config.get('mcp_servers'), 'native_host_configuration_refused')
        need(not config.get('hooks'), 'native_host_configuration_refused')
        features = config.get('features',{})
        need(all(features.get(name) is False for name in DISABLED_FEATURES),
             'native_host_configuration_refused')
        need(config.get('web_search') == 'disabled', 'native_host_configuration_refused')

    async def send(self, value):
        self.check()
        raw = json.dumps(value,separators=(',',':'),allow_nan=False).encode('utf-8')
        need(len(raw) <= FRAME_LIMIT, 'native_executor_protocol_error')
        self.process.stdin.write(raw+b'\n')
        await self.process.stdin.drain()

    async def receive(self):
        self.check()
        raw = await asyncio.wait_for(self.process.stdout.readline(),max(0.001,self.deadline-time.time()))
        need(raw and raw.endswith(b'\n') and len(raw) <= FRAME_LIMIT, 'native_executor_protocol_error')
        value = json.loads(raw)
        need(type(value) is dict, 'native_executor_protocol_error')
        self.check()
        if value.get('method') == 'turn/completed':
            turn = value.get('params',{}).get('turn',{})
            need(type(turn.get('id')) is str, 'native_executor_protocol_error')
            self.terminals[turn['id']] = turn
        elif 'id' in value and 'method' in value:
            # Never turn an untrusted runtime question into a user approval or
            # an administrative application call from this worker.
            await self.send({'id':value['id'],'error':{'code':-32601,'message':'Unsupported worker interaction'}})
        return value

    async def request(self, method, params):
        self.request_id += 1
        selected = self.request_id
        await self.send({'id':selected,'method':method,'params':params})
        while True:
            value = await self.receive()
            if value.get('id') == selected and 'method' not in value:
                need('error' not in value and type(value.get('result')) is dict, 'native_runtime_refused')
                return value['result']

    async def run(self, relay, *, environment_id, guest_cwd, goal, repair_context=None):
        await self.request('environment/add',{'environmentId':environment_id,
            'execServerUrl':relay.url,'authBearerToken':relay.token,'connectTimeoutMs':3000})
        status = await self.request('environment/status',{'environmentId':environment_id})
        # environment/info initializes a new lazy connection; status alone
        # deliberately does not start or recover it.
        if status.get('status') == 'pending':
            await self.request('environment/info',{'environmentId':environment_id})
        status = await self.request('environment/status',{'environmentId':environment_id})
        need(status.get('status') == 'ready' and not relay.failed, 'native_executor_unavailable')
        thread = await self.request('thread/start',{'model':self.model,'modelProvider':'sovite',
            'cwd':str(self.home),'ephemeral':True,'approvalPolicy':'never','sandbox':'workspace-write',
            'allowProviderModelFallback':False,
            'environments':[{'environmentId':environment_id,'cwd':guest_cwd,'runtimeWorkspaceRoots':[guest_cwd]}]})
        thread_id = thread['thread']['id']
        params = {'threadId':thread_id,'input':[{'type':'text','text':goal}]}
        if repair_context is not None:
            params['additionalContext'] = {'owner_repair_feedback':{'kind':'untrusted',
                'name':'independent_check_feedback',
                'value':json.dumps(repair_context,sort_keys=True,separators=(',',':'),allow_nan=False)}}
        response = await self.request('turn/start',params)
        turn_id = response['turn']['id']
        while turn_id not in self.terminals:
            need(not relay.failed, 'external_execution_uncertain')
            await self.receive()
        need(self.terminals[turn_id].get('status') == 'completed' and not relay.failed,
             'native_runtime_failed')
        return {'thread_id':thread_id,'turn_id':turn_id}

    async def close(self):
        if self.process is not None:
            return await stop_process(self.process)
