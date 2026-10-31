"""Fixed stdlib transport inside the private native supervisor namespace.

No destinations, credentials or execution language are selected from native
input. The only peer paths are two readonly-mounted attempt socket inodes.
"""
from __future__ import annotations
import asyncio
import sys
import math
import time

MODEL_SOCKET='/run/sovite-native/model.sock'
EXECUTOR_SOCKET='/run/sovite-native/executor.sock'
MODEL_PORT=18271
EXECUTOR_PORT=18272


async def pipe(reader,writer):
    try:
        while block:=await reader.read(65536):
            writer.write(block);await writer.drain()
    finally:
        writer.close()


async def run(command,deadline):
    tasks=set()
    async def accept(reader,writer,path):
        task=asyncio.current_task()
        if len(tasks)>=8:
            writer.close();return
        tasks.add(task)
        try:
            remote,output=await asyncio.open_unix_connection(path)
            copies=[asyncio.create_task(pipe(reader,output)),asyncio.create_task(pipe(remote,writer))]
            try:
                await asyncio.wait(copies,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for copy in copies:copy.cancel()
                await asyncio.gather(*copies,return_exceptions=True)
        except (OSError,ConnectionError):writer.close()
        finally:tasks.discard(task)
    model=await asyncio.start_server(lambda r,w:accept(r,w,MODEL_SOCKET),'127.0.0.1',MODEL_PORT)
    executor=await asyncio.start_server(lambda r,w:accept(r,w,EXECUTOR_SOCKET),'127.0.0.1',EXECUTOR_PORT)
    process=None
    try:
        # Native app-server stdin/stdout remain the original protected pipes.
        process=await asyncio.create_subprocess_exec(*command)
        try:return await asyncio.wait_for(process.wait(),max(.001,deadline-time.time()))
        except TimeoutError:return 124
    finally:
        model.close();executor.close()
        for task in list(tasks):task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await model.wait_closed();await executor.wait_closed()
        if process is not None and process.returncode is None:
            process.kill();await process.wait()


def main():
    if len(sys.argv)<5 or sys.argv[1]!='--deadline' or sys.argv[3]!='--':raise SystemExit(78)
    deadline=float(sys.argv[2])
    if not math.isfinite(deadline) or deadline<=time.time():raise SystemExit(78)
    raise SystemExit(asyncio.run(run(sys.argv[4:],deadline)))


if __name__=='__main__':main()
