"""Always-on native supervisor OS boundary and two fixed guarded transports."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from urllib.parse import urlsplit

from .adapters import RuntimeFailure

_PROFILE=Path('/run/current-system/sw/share/sovite/native-supervisor.json')
_CHILD=Path(__file__).with_name('repository_native_isolation_child.py')
_STORE=re.compile(r'/nix/store/[a-z0-9]{32}-[^/]+\Z')
MODEL_ORIGIN='http://127.0.0.1:18271'
EXECUTOR_URL='ws://127.0.0.1:18272'


def need(value):
    if not value:raise RuntimeFailure('native_supervisor_unavailable')


def protected(path,*,limit=1024*1024):
    path=Path(path).resolve(strict=True)
    need(str(path).startswith('/nix/store/'))
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o222
             and info.st_size<=limit)
        with os.fdopen(fd,'rb',closefd=False)as stream:raw=stream.read(limit+1)
        after=os.fstat(fd)
        need(len(raw)==info.st_size and len(raw)<=limit
             and (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)==
                 (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns))
        return path,raw
    finally:os.close(fd)


@dataclass(frozen=True)
class Profile:
    sha256:str
    bubblewrap:str
    python:str
    tools:str
    store_paths:tuple[str,...]
    child:str


def load_profile():
    try:
        _,raw=protected(_PROFILE)
        def unique(pairs):
            result={}
            for key,value in pairs:need(key not in result);result[key]=value
            return result
        value=json.loads(raw,object_pairs_hook=unique)
        fields={'schema_version','bubblewrap','bubblewrap_sha256','python','python_sha256',
                'tools','store_paths','child_sha256'}
        need(type(value)is dict and set(value)==fields
             and value['schema_version']=='sovite-native-supervisor/v1')
        roots=value['store_paths']
        need(type(roots)is list and 1<=len(roots)<=4096 and roots==sorted(set(roots))
             and all(type(root)is str and _STORE.fullmatch(root)for root in roots))
        for root in roots:
            info=Path(root).lstat()
            need(info.st_uid==0 and not info.st_mode&0o222 and not stat.S_ISLNK(info.st_mode))
        for name in ('bubblewrap','python'):
            requested=Path(value[name])
            need(requested.is_absolute() and any(str(requested).startswith(root+'/')for root in roots))
            actual,data=protected(requested,limit=32*1024*1024)
            need(any(str(actual).startswith(root+'/')for root in roots)
                 and hashlib.sha256(data).hexdigest()==value[name+'_sha256']
                 and os.access(actual,os.X_OK))
        tools=Path(value['tools']);need(str(tools)in roots and (tools/'bin').is_dir())
        child,data=protected(_CHILD,limit=64*1024)
        need(hashlib.sha256(data).hexdigest()==value['child_sha256'])
        return Profile(hashlib.sha256(raw).hexdigest(),value['bubblewrap'],value['python'],
                       str(tools),tuple(roots),str(child))
    except RuntimeFailure:raise
    except Exception:raise RuntimeFailure('native_supervisor_unavailable')from None


def endpoint(origin,scheme):
    try:
        parsed=urlsplit(origin)
        need(parsed.scheme==scheme and parsed.hostname=='127.0.0.1' and parsed.port
             and not parsed.username and not parsed.password and parsed.path in {'','/'}
             and not parsed.query and not parsed.fragment)
        return parsed.port
    except (ValueError,TypeError):raise RuntimeFailure('native_supervisor_unavailable')from None


class Namespace:
    def __init__(self,home,model_origin,deadline,authorization_check):
        self.profile=load_profile()
        self.home=Path(home)
        self.deadline=deadline
        self.authorization_check=authorization_check
        self.model_port=endpoint(model_origin,'http')
        self.executor_port=None
        self.root=None;self.servers=[];self.tasks=set();self.failed=False

    def check(self):
        self.authorization_check()
        need(time.time()<self.deadline and not self.failed)
        need(load_profile()==self.profile)

    def bind_executor(self,relay):
        # Protected composition supplies the actual existing relay, never a URL
        # from native output/configuration or an alternate routing registry.
        from .repository_native_runtime import ExecutorRelay
        need(isinstance(relay,ExecutorRelay))
        port=endpoint(relay.url,'ws')
        need(self.executor_port in {None,port})
        self.executor_port=port

    async def _accept(self,reader,writer,name):
        task=asyncio.current_task()
        if len(self.tasks)>=8:
            writer.close();return
        self.tasks.add(task)
        remote_writer=None
        try:
            self.check()
            port=self.model_port if name=='model' else self.executor_port
            need(port is not None)
            remote,remote_writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',port),
                max(.001,self.deadline-time.time()))
            self.check()
            async def copy(source,target):
                while True:
                    self.check()
                    block=await asyncio.wait_for(source.read(65536),max(.001,self.deadline-time.time()))
                    self.check()
                    if not block:return
                    target.write(block)
                    await asyncio.wait_for(target.drain(),max(.001,self.deadline-time.time()))
            copies=[asyncio.create_task(copy(reader,remote_writer)),asyncio.create_task(copy(remote,writer))]
            try:
                done,_=await asyncio.wait(copies,return_when=asyncio.FIRST_COMPLETED)
                for copy in done:copy.result()
            finally:
                for copy in copies:copy.cancel()
                await asyncio.gather(*copies,return_exceptions=True)
        except BaseException as error:
            if not isinstance(error,asyncio.CancelledError):self.failed=True
        finally:
            writer.close()
            if remote_writer is not None:remote_writer.close()
            self.tasks.discard(task)

    async def start(self,command,env):
        self.check()
        self.root=Path(tempfile.mkdtemp(prefix='sovite-native-',dir='/tmp'))
        self.root.chmod(0o700)
        try:
            for name in ('model','executor'):
                path=self.root/(name+'.sock')
                server=await asyncio.start_unix_server(lambda r,w,n=name:self._accept(r,w,n),path=str(path))
                path.chmod(0o600);self.servers.append(server)
            profile=self.profile
            argv=[profile.bubblewrap,'--unshare-all','--unshare-user','--new-session','--die-with-parent',
                  '--disable-userns','--cap-drop','ALL']
            for root in profile.store_paths:argv+=['--ro-bind',root,root]
            argv+=['--proc','/proc','--dev','/dev','--tmpfs','/tmp','--dir','/run/sovite-native',
                '--ro-bind',str(self.root/'model.sock'),'/run/sovite-native/model.sock',
                '--ro-bind',str(self.root/'executor.sock'),'/run/sovite-native/executor.sock',
                '--dir','/run/current-system/sw','--ro-bind',profile.tools+'/bin','/run/current-system/sw/bin',
                '--bind',str(self.home),str(self.home),'--chdir',str(self.home),
                '--ro-bind',profile.child,'/run/sovite-native/relay.py','--remount-ro','/',
                profile.python,'-I','-B','/run/sovite-native/relay.py','--deadline',str(self.deadline),'--',*command]
            return await asyncio.create_subprocess_exec(*argv,cwd=self.home,env=env,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,limit=4*1024*1024+1)
        except (OSError,ValueError):
            await self.close()
            raise RuntimeFailure('native_supervisor_unavailable')from None
        except BaseException:
            await self.close();raise

    async def close(self):
        for server in self.servers:server.close()
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        for server in self.servers:await server.wait_closed()
        self.servers=[]
        if self.root is not None:
            shutil.rmtree(self.root)
            self.root=None
