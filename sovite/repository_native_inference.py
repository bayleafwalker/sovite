"""Fixed per-attempt inference bridge outside the candidate VM.

Existing Sovite model publication and live authorization own the destination.
This bridge adds neither model routing nor a credential registry. Unknown
accounting stops the attempt and retains its durable budget reservation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal, InvalidOperation
import hmac
import json
import math
import secrets
import socket
import time
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

from .adapters import RuntimeFailure, model_verify, token_headers
from .connector.helper import validate_body
from .connector.protocol import BODY_LIMIT
from .model_gateway_client import ModelGatewayInference
from .repository_native_runtime import check_tools, check_response_tools, need
from .repository_sessions import _unique_pairs


class DirectResponses(ModelGatewayInference):
    """Existing direct owner-published API model, not a fabricated weight helper."""
    def __init__(self, context):
        model = context.model
        endpoint = model.get('endpoint','')
        parsed = urlsplit(endpoint)
        need(model.get('adapter') == 'openai-compatible' and model.get('wire_api') == 'responses'
             and model.get('connection',{}).get('kind') != 'model-gateway'
             and parsed.scheme == 'https' and parsed.hostname and not parsed.username
             and not parsed.password and not parsed.query and not parsed.fragment,
             'native_model_adapter_unavailable')
        timeout = model.get('timeout_seconds',120)
        need(type(timeout) is int and 1 <= timeout <= 600,'native_model_adapter_unavailable')
        self.direct_model = model
        super().__init__(origin=endpoint.rstrip('/'), token='',verify=model_verify(model),
            binding_id=model['id'],model_id=model['model'],generation=context.generation,
            job_id=context.job_id,fence=context.fence,deadline=context.deadline,
            capability='',workload='',binding_timeout=timeout)
        token_headers(model)  # Existing explicit credential contract, protected side only.

    def _responses_destination(self, request_deadline):
        headers = {**token_headers(self.direct_model),'Content-Type':'application/json','Accept':'text/event-stream'}
        return self.origin+'/responses',headers


def transport_for(context):
    model = context.model
    if model.get('connection',{}).get('kind') == 'model-gateway':
        return ModelGatewayInference.from_environment(model=model,generation=context.generation,
            job_id=context.job_id,fence=context.fence,deadline=context.deadline,execution=context.execution)
    return DirectResponses(context)


class Accounting:
    """Owner-reviewed fee/limit facts, which qualification must bind exactly.

    No subscription or unknown-price fallback exists. The reservation ceiling
    must include the actual model/provider maximum billable input-token bound
    (not a token estimate from JSON bytes) and current quota/price evidence before a real
    deployment can be qualified; supplying these numbers is not that evidence.
    """
    def __init__(self, value, model_id):
        need(type(value) is dict and set(value) == {'model_id','valid_until','maximum_request_bytes',
            'maximum_input_tokens','maximum_output_tokens','reservation_usd','input_usd_per_million','output_usd_per_million'},
            'native_model_accounting_unavailable')
        need(value['model_id'] == model_id and type(value['valid_until']) in {int,float}
             and math.isfinite(value['valid_until']) and value['valid_until'] > time.time(),
             'native_model_accounting_unavailable')
        need(type(value['maximum_request_bytes']) is int and 1 <= value['maximum_request_bytes'] <= BODY_LIMIT
             and type(value['maximum_input_tokens']) is int and 1 <= value['maximum_input_tokens'] <= 16*1024*1024
             and type(value['maximum_output_tokens']) is int and 1 <= value['maximum_output_tokens'] <= 32768,
             'native_model_accounting_unavailable')
        self.value = value
        try:
            self.reservation = Decimal(str(value['reservation_usd']))
            self.input_rate = Decimal(str(value['input_usd_per_million']))
            self.output_rate = Decimal(str(value['output_usd_per_million']))
            need(all(number.is_finite() and number > 0 for number in
                     (self.reservation,self.input_rate,self.output_rate)), 'native_model_accounting_unavailable')
            ceiling = (self.input_rate*value['maximum_input_tokens'] +
                       self.output_rate*value['maximum_output_tokens'])/Decimal(1000000)
            need(self.reservation >= ceiling,'native_model_accounting_unavailable')
        except (InvalidOperation,ValueError):
            raise RuntimeFailure('native_model_accounting_unavailable') from None

    def payload(self, source):
        need(time.time() < self.value['valid_until'],'native_model_accounting_unavailable')
        raw = json.dumps(source,allow_nan=False,separators=(',',':')).encode('utf-8')
        need(len(raw) <= self.value['maximum_request_bytes'],'native_model_accounting_unavailable')
        result = dict(source)
        limit = result.setdefault('max_output_tokens',self.value['maximum_output_tokens'])
        need(type(limit) is int and 1 <= limit <= self.value['maximum_output_tokens'],
             'native_model_accounting_unavailable')
        return result

    def cost(self, usage):
        need(usage is not None and usage['input_tokens'] <= self.value['maximum_input_tokens']
             and usage['output_tokens'] <= self.value['maximum_output_tokens'],'external_execution_uncertain')
        amount = (Decimal(usage['input_tokens'])*self.input_rate +
                  Decimal(usage['output_tokens'])*self.output_rate)/Decimal(1000000)
        result = float(amount)
        need(math.isfinite(result) and result >= 0,'external_execution_uncertain')
        return result


class NativeInferenceBridge:
    def __init__(self, context, budget, authorization_check, accounting, *, transport=None):
        self.context, self.budget = context, budget
        self.authorization_check = authorization_check
        self.accounting = Accounting(accounting,context.model['id'])
        self.transport = transport or transport_for(context)
        self.token = secrets.token_urlsafe(32)
        self.failure = asyncio.Event()
        self.failed = False
        self.app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        self.serial = asyncio.Lock()
        self.active = set()
        self.app.post('/v1/responses')(self._responses)

    async def _responses(self, request: Request):
        raw_token = request.headers.get('authorization','').encode('utf-8')
        if not hmac.compare_digest(raw_token,('Bearer '+self.token).encode('ascii')):
            return JSONResponse({'error':{'message':'Native inference unavailable'}},status_code=403)
        task = asyncio.current_task()
        self.active.add(task)
        try:
            async with asyncio.timeout(max(.001,self.context.deadline-time.time())):
                return await self._execute(request)
        except asyncio.CancelledError:
            self.failed = True
            self.failure.set()
            raise
        except Exception:
            self.failed = True
            self.failure.set()
            # Do not echo upstream messages, credentials, or candidate content.
            return JSONResponse({'error':{'message':'Native inference unavailable'}},status_code=409)
        finally:
            self.active.discard(task)

    async def _execute(self, request):
        need(not self.failed,'native_inference_unavailable')
        self.authorization_check()
        need(time.time() < self.context.deadline,'execution_timeout')
        body = bytearray()
        async for block in request.stream():
            body.extend(block)
            need(len(body) <= BODY_LIMIT,'model_protocol_error')
        value = json.loads(body,object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: need(False,'model_protocol_error'))
        need(type(value) is dict and value.get('model') == self.context.model['model'],
             'model_protocol_error')
        check_tools(value)
        payload = self.accounting.payload(value)
        forwarded = {key:item for key,item in payload.items()
                     if key not in {'client_metadata','prompt_cache_key'}}
        validate_body('responses',json.dumps(forwarded,allow_nan=False).encode(),self.context.model['model'])
        call_id = 'native-model-'+uuid.uuid4().hex
        async with self.serial:
            need(not self.failed,'native_inference_unavailable')
            self.authorization_check()
            self.budget.acquire_slot(call_id)
            self.budget.reserve_cost(call_id,float(self.accounting.reservation))
            # From this point every failure retains the reservation/slot;
            # never replay an ambiguous provider operation or settle $0.
            result = await self.transport.responses(self.context.model,payload,self.authorization_check)
            self.authorization_check()
            check_response_tools(result.response,result.events)
            need(time.time() < self.context.deadline,'execution_timeout')
            self.budget.settle_cost(call_id,self.accounting.cost(result.usage))
            self.budget.release_slot(call_id)
            self.authorization_check()
            return Response(result.sse,media_type='text/event-stream',headers={'Cache-Control':'no-store'})

    @asynccontextmanager
    async def listen(self):
        class NoSignalsServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                yield
        listener = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]
        self.origin = f'http://127.0.0.1:{port}'
        server = NoSignalsServer(uvicorn.Config(self.app,host='127.0.0.1',port=port,
            access_log=False,log_level='critical',lifespan='off',timeout_keep_alive=1))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(min(5,max(.001,self.context.deadline-time.time()))):
                while not server.started:
                    need(not task.done(),'native_inference_unavailable')
                    await asyncio.sleep(.01)
            yield self
        finally:
            self.failed = True
            self.failure.set()
            active = list(self.active)
            for operation in active:
                operation.cancel()
            await asyncio.gather(*active,return_exceptions=True)
            server.should_exit = True
            try:
                await asyncio.wait_for(task,3)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            listener.close()
