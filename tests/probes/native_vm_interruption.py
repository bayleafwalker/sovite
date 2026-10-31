"""Unqualified actual-native fault fixture; model stream stops after a guest edit.

The existing protocol probe supplies the exact synthetic first tool call. This
separate probe cancels the protected loop after the guest result reaches the
second model request and before that request has a terminal response or usage.
Root-owner quiescence, sealed export and fresh-reader recovery run separately.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import native_vm_protocol as fixture


class InterruptedModel(fixture.SyntheticModel):
    def __init__(self,*args):
        super().__init__(*args)
        self.edited=asyncio.Event()
    @asynccontextmanager
    async def listen(self):
        server=await asyncio.start_server(self.handle,'127.0.0.1',0)
        self.origin=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        try:yield self
        finally:
            server.close()
            # The intentionally incomplete stream must be cancelled before
            # Python 3.12 waits for every accepted connection to close.
            handlers=list(self.handlers)
            for task in handlers:task.cancel()
            await asyncio.gather(*handlers,return_exceptions=True)
            await server.wait_closed()
    async def chunks(self,payload):
        if len(self.requests)==1:
            async for block in super().chunks(payload):yield block
            return
        assert len(self.requests)==2
        outputs=[item.get('output','') for item in payload.get('input',[])
                 if item.get('type')=='function_call_output']
        assert any('guest-isolated' in output for output in outputs),outputs
        self.runtime.check_tools(payload)
        assert fixture.TOKEN not in json.dumps(payload)
        yield fixture.event('response.created',response={'id':'resp_interrupted',
            'model':fixture.MODEL,'status':'in_progress','output':[]})
        self.edited.set()
        await asyncio.Event().wait()


async def probe(args):
    runtime=fixture.load_runtime(args.runtime_module)
    version=subprocess.run([str(args.codex),'--version'],check=True,capture_output=True,text=True).stdout.strip()
    assert version==runtime.VERSION
    assert not fixture.HOME.exists()
    fixture.HOME.mkdir(mode=0o700)
    (fixture.HOME/'supervisor-only.txt').write_text('public fixture supervisor-only marker')
    os.environ['SOVITE_NATIVE_TEST_HOST_SECRET']='public-fixture-host-only-sentinel'
    argv=json.loads(args.ssh_argv_json.read_bytes())
    assert type(argv)is list and argv and all(type(item)is str for item in argv)
    async def process():
        return await asyncio.create_subprocess_exec(*argv,'exec-v1',stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,limit=runtime.FRAME_LIMIT+1)
    model=InterruptedModel(runtime,args.guest_cwd)
    deadline=time.time()+90
    relay=runtime.ExecutorRelay(process,deadline,lambda:True)
    async with model.listen(),relay.listen():
        app=runtime.NativeAppServer(args.codex,fixture.HOME,fixture.MODEL,model.origin,
                                    fixture.TOKEN,deadline,lambda:True)
        operation=None
        try:
            await app.start()
            operation=asyncio.create_task(app.run(relay,environment_id='interrupted-kvm-fixture',
                guest_cwd=args.guest_cwd,goal=fixture.GOAL))
            edited=asyncio.create_task(model.edited.wait())
            done,_=await asyncio.wait({operation,edited},timeout=60,return_when=asyncio.FIRST_COMPLETED)
            assert edited in done and not operation.done(),'Native loop completed before interruption barrier'
            operation.cancel()
            try:await operation
            except asyncio.CancelledError:pass
            else:raise AssertionError('Interrupted native loop returned success')
            assert len(model.requests)==2 and not relay.failed
        finally:
            if operation is not None and not operation.done():operation.cancel()
            await relay.close();await app.close()
    assert relay.exit_code==0 and not relay.failed
    assert not any((fixture.HOME/name).exists() for name in ('native-shell-marker.txt',
        'native-pixel.png','candidate-config-escape'))
    print(json.dumps({'schema_version':'native-guest-interruption-fixture/v1','qualified':False,
        'runtime_sha256':hashlib.sha256(args.runtime_module.read_bytes()).hexdigest(),
        'native_sha256':hashlib.sha256(args.codex.read_bytes()).hexdigest(),
        'native_version':version,'model_requests':2,'paid_model_calls':0,
        'native_turn_completed':False,'interrupted_after_guest_edit':True,
        'second_response_completed':False,'second_response_usage':None},sort_keys=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-module',required=True,type=Path)
    parser.add_argument('--codex',required=True,type=Path)
    parser.add_argument('--ssh-argv-json',required=True,type=Path)
    parser.add_argument('--guest-cwd',default='/workspace/repository')
    asyncio.run(asyncio.wait_for(probe(parser.parse_args()),105))


if __name__=='__main__':main()
