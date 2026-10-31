"""Fixed health projection and local diskless-lab helper; no candidate arguments."""
from __future__ import annotations

import asyncio
import hashlib
import ssl
import time

import httpx

from .resource_contract.runtime import validate_schema
from .resource_gateway_store import canonical
from .resource_target_catalog import decode, installed_public_bytes, need


class FixedTargetExecutors:
    async def health(self, target):
        ca=installed_public_bytes(target['ca_file'],limit=65536)
        need(hashlib.sha256(ca).hexdigest()==target['ca_sha256'],'resource_catalog_changed')
        tls=ssl.create_default_context(cadata=ca.decode('ascii'))
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(verify=tls,trust_env=False,follow_redirects=False,timeout=5) as client:
                async with client.stream('GET',target['origin']+'/healthz') as response:
                    need(response.status_code==200,'resource_health_unavailable')
                    raw=bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part);need(len(raw)<=1024,'resource_health_unavailable')
        validate_schema('PeerHealthSource',decode(raw))
        return True

    async def lab(self, request):
        validate_schema('LabHostRequest',request)
        encoded=canonical(request)+b'\n';need(len(encoded)<=4096)
        process=None
        try:
            async with asyncio.timeout(5):
                process=await asyncio.create_subprocess_exec('/run/current-system/sw/bin/cloud-kvm-lab',
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                    env={'PATH':'/run/current-system/sw/bin'},limit=4097)
                process.stdin.write(encoded);await process.stdin.drain();process.stdin.close()
                # Bounded read then wait; communicate() could accumulate arbitrary output.
                raw=await process.stdout.read(4097);need(len(raw)<=4096,'resource_helper_invalid')
                need(not await process.stdout.read(1),'resource_helper_invalid')
                await process.wait();need(process.returncode==0,'resource_helper_unavailable')
            value=decode(raw);validate_schema('LabHostObservation',value)
            for key in ('operation_id','create_operation_id','template_id','template_sha256','deadline'):
                need(value[key]==request[key],'resource_helper_invalid')
            need(value['state']!='absent' or time.time()>=request['deadline'],'resource_helper_invalid')
            expected='cloud-kvm-lab-'+hashlib.sha256(request['create_operation_id'].encode()).hexdigest()+'.service'
            need(value['unit']==expected,'resource_helper_invalid')
            need(value['created_boot_id'] in {None,value['host_boot_id']} or value['state']=='unknown',
                 'resource_helper_invalid')
            need(value['state']!='running' or (value['invocation_id'] is not None
                 and value['created_boot_id']==value['host_boot_id']),'resource_helper_invalid')
            return value
        finally:
            if process is not None and process.returncode is None:
                process.kill();await process.wait()
