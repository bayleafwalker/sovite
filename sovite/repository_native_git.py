"""Bounded trusted Git parsing after the resource owner confirms quiescence."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import stat
import time

from .repository_native_runtime import need, stop_process

OUTPUT_LIMIT = 8 * 1024 * 1024
EXPANDED_LIMIT = 8 * 1024 * 1024
_PROCESS_LIMIT = 64


def _tool(name):
    path=Path('/run/current-system/sw/bin')/name
    path=path.resolve(strict=True)
    info=path.stat()
    need(str(path).startswith('/nix/store/') and path.name==name
         and stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022)
    return path


def tool_facts():
    return {name:{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            for name in ('git','prlimit') for path in [_tool(name)]}


def tools_current(facts):
    return all(str(_tool(name)) == facts[name]['path'] for name in ('git','prlimit'))


def _git_exec_path(binary):
    return binary.parent.parent/'libexec/git-core'


class NativeGit:
    def __init__(self, deadline, authorization_check, *, expected_tools=None):
        self.deadline,self.authorization_check=deadline,authorization_check
        self.binary=_tool('git')
        self.prlimit=_tool('prlimit')
        need(expected_tools is None or tool_facts() == expected_tools,'native_git_qualification_changed')
        self.exec_path=_git_exec_path(self.binary)
        need(self.exec_path.is_dir())

    def check(self):
        self.authorization_check()
        need(time.time()<self.deadline,'execution_timeout')

    async def __call__(self,*args):
        self.check()
        environment={'PATH':str(self.binary.parent),'LANG':'C','HOME':'/nonexistent',
            'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_SYSTEM':'/dev/null','GIT_CONFIG_GLOBAL':'/dev/null',
            'GIT_TERMINAL_PROMPT':'0','GIT_OPTIONAL_LOCKS':'0','GIT_NO_LAZY_FETCH':'1',
            'GIT_NO_REPLACE_OBJECTS':'1','GIT_GRAFT_FILE':'/dev/null',
            'GIT_EXEC_PATH':str(self.exec_path),'GIT_PAGER':'cat'}
        options=['-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
            '-c','core.attributesFile=/dev/null','-c','protocol.allow=never',
            '-c','protocol.file.allow=always','-c','fetch.unpackLimit=1','-c','transfer.unpackLimit=1',
            '-c','fetch.fsckObjects=true','-c','transfer.fsckObjects=true','-c','pack.windowMemory=32m','-c','pack.threads=1','-c','pack.packSizeLimit=16m',
            '-c','gc.auto=0','-c','maintenance.auto=false','--no-pager']
        # prlimit applies hard+soft limits before exec without a Python fork
        # callback in the supervisor's potentially multithreaded process.
        limits=['--cpu=8:8','--as=536870912:536870912','--fsize=83886080:83886080',
                f'--nproc={_PROCESS_LIMIT}:{_PROCESS_LIMIT}','--nofile=64:64','--core=0:0']
        process=await asyncio.create_subprocess_exec(str(self.prlimit),*limits,'--',str(self.binary),*options,*args,
            cwd='/',env=environment,stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        async def consume():
            result=bytearray()
            while block:=await process.stdout.read(65536):
                self.check()
                need(len(result)+len(block)<=OUTPUT_LIMIT,'native_git_output_too_large')
                result.extend(block)
            await process.wait()
            self.check()
            need(process.returncode==0,'native_git_refused')
            return bytes(result)
        async def watch():
            while True:
                await asyncio.sleep(.1)
                self.check()
        operation=asyncio.create_task(consume());watchdog=asyncio.create_task(watch())
        try:
            done,_=await asyncio.wait({operation,watchdog},timeout=max(.001,self.deadline-time.time()),
                                      return_when=asyncio.FIRST_COMPLETED)
            if watchdog in done:watchdog.result()
            need(operation in done,'execution_timeout')
            return await operation
        finally:
            for task in (operation,watchdog):task.cancel()
            await asyncio.gather(operation,watchdog,return_exceptions=True)
            if process.returncode is None:await stop_process(process,grace=0)
