"""Guards around the emitted openapi-python-client transport."""
from dataclasses import dataclass
from contextvars import ContextVar
from importlib import import_module
from importlib import resources
import json
import math
import re
from urllib.parse import unquote_to_bytes
from vuoro_contracts import (ABSENT, ContractBundle, ContractError, parse_wire_json,
                             validate_wire_json_limit)

@dataclass(frozen=True)
class Envelope:
    status: int
    body: object
    content: bytes

def create_validated_client(*, client, integrity_sha256):
    bundle = ContractBundle.load(
        resources.files(__package__).joinpath("data"),
        integrity_sha256=integrity_sha256,
    )
    manifest = bundle.manifest
    ops = manifest["operations"]
    native_bindings = {'repositoryRepairFeedbackContext': {'module': 'api.default.repository_repair_feedback_context', 'parameters': {}, 'model': {'module': 'feedback_context_request', 'class': 'FeedbackContextRequest'}, 'body_parameter': 'body'}, 'repositoryRepairSubmit': {'module': 'api.default.repository_repair_submit', 'parameters': {}, 'model': {'module': 'repair_submission', 'class': 'RepairSubmission'}, 'body_parameter': 'body'}}
    transport = client.get_httpx_client()
    configured_base = transport.base_url
    current = ContextVar("vuoro_contract_current", default=None)
    def effective_origin(url):
        scheme=url.scheme.lower()
        host=url.host.lower()
        port=url.port
        if port is None:
            port=443 if scheme=="https" else 80 if scheme=="http" else None
        if scheme not in ("http","https") or not host or port is None:
            raise ContractError("operation")
        return scheme,host,port
    def request_raw_path(request):
        try:
            if (configured_base.username or configured_base.password or configured_base.query or
                    configured_base.fragment or request.url.username or request.url.password):
                raise ContractError("operation")
            if effective_origin(request.url)!=effective_origin(configured_base):
                raise ContractError("operation")
            raw=request.url.raw_path.partition(b"?")[0]
            base=configured_base.raw_path.partition(b"?")[0].rstrip(b"/")
            if base:
                if raw!=base and not raw.startswith(base+b"/"):
                    raise ContractError("operation")
                raw=raw[len(base):] or b"/"
            return raw
        except ContractError:
            raise
        except Exception:
            raise ContractError("operation") from None
    def raw_path_segments(request):
        raw=request_raw_path(request)
        try: return [part.decode("ascii","strict") for part in raw.split(b"/")]
        except Exception: raise ContractError("parameter") from None
    def decode_segment(raw):
        try: return unquote_to_bytes(raw).decode("utf-8","strict")
        except Exception: raise ContractError("parameter") from None
    def parameter_kind(identifier):
        seen=set()
        schema=bundle.schemas.get(identifier)
        while isinstance(schema,dict):
            kind=schema.get("type")
            if isinstance(kind,str): return kind
            reference=schema.get("$ref")
            if not isinstance(reference,str) or reference in seen: return None
            seen.add(reference)
            schema=bundle.schemas.get(reference)
        return None
    def decode_parameter(identifier,raw):
        kind=parameter_kind(identifier)
        if kind=="integer":
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)",raw):
                raise ContractError("parameter")
            try: return int(raw)
            except Exception: raise ContractError("parameter") from None
        if kind=="number":
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?",raw):
                raise ContractError("parameter")
            try:
                value=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if type(value) not in (int,float) or (type(value) is float and not math.isfinite(value)):
                    raise ValueError
                return value
            except Exception: raise ContractError("parameter") from None
        if kind=="boolean":
            if raw=="true": return True
            if raw=="false": return False
            raise ContractError("parameter")
        return raw
    def same_parameter(left,right):
        return type(left) is type(right) and left==right
    def resolve(request):
        hits = []
        actual=raw_path_segments(request)
        for name, op in ops.items():
            parts=op["path"].split("/")
            if len(parts)!=len(actual) or op["method"]!=request.method: continue
            matched=True
            for template,raw in zip(parts,actual):
                if template.startswith("{") and template.endswith("}"):
                    if not raw: matched=False;break
                elif decode_segment(raw)!=template: matched=False;break
            if matched:hits.append((name,op))
        if len(hits) != 1: raise ContractError("operation")
        return hits[0]
    def decoded_parameters(request,name,op):
        expected=current.get()
        if expected is not None and expected["operation"]!=name: raise ContractError("operation")
        values={"path":{},"query":{},"header":{}}
        parts=op["path"].split("/"); actual=raw_path_segments(request)
        if len(parts)!=len(actual): raise ContractError("parameter")
        for template,raw in zip(parts,actual):
            if template.startswith("{") and template.endswith("}"):
                key=template[1:-1]
                parameter=next((p for p in op["parameters"] if p["in"]=="path" and p["name"]==key),None)
                if parameter is None: raise ContractError("parameter")
                value=decode_parameter(parameter["schema"],decode_segment(raw))
                values["path"][key]=value
            elif template!=decode_segment(raw): raise ContractError("parameter")
        for key,value in request.url.params.multi_items(): values["query"].setdefault(key,[]).append(value)
        for name_,field in ((p["name"],p) for p in op["parameters"] if p["in"]=="header"):
            found=request.headers.get_list(name_)
            if found: values["header"][name_]=found
        declared={p["in"]:set() for p in op["parameters"]}
        for p in op["parameters"]: declared[p["in"]].add(p["name"])
        if set(values["path"])!=declared.get("path",set()) or not set(values["query"])<=declared.get("query",set()): raise ContractError("parameter")
        for location in ("path","query","header"):
            for key in declared.get(location,set()):
                param=next(p for p in op["parameters"] if p["in"]==location and p["name"]==key)
                wire=values[location].get(key)
                supplied=expected is not None and key in expected["parameters"]
                if wire is None:
                    if param["required"] or supplied: raise ContractError("parameter")
                    continue
                if location=="path":
                    value=wire
                elif location=="query":
                    if len(wire)!=1: raise ContractError("parameter")
                    value=decode_parameter(param["schema"],wire[0])
                elif location=="header":
                    if len(wire)!=1: raise ContractError("parameter")
                    value=decode_parameter(param["schema"],wire[0])
                bundle.validate_parameter(name,location,key,value)
                if expected is not None:
                    original=expected["parameters"].get(key,ABSENT)
                    if original is ABSENT or not same_parameter(original,value): raise ContractError("parameter")
        allowed_headers={"host","accept","accept-encoding","connection","user-agent","content-length","content-type"}
        allowed_headers|={p["name"].lower() for p in op["parameters"] if p["in"]=="header"}
        if set(request.headers.keys())-allowed_headers: raise ContractError("parameter")
        return values
    def on_request(request):
        name, op = resolve(request)
        decoded_parameters(request,name,op)
        raw = bytes(request.content)
        if op["request"] is None:
            if raw: raise ContractError("schema_value")
            value = ABSENT
        else:
            request_cap = validate_wire_json_limit(op["requestMaxBytes"])
            value = parse_wire_json(raw, max_bytes=request_cap)
        bundle.validate_request(name, value)
    def on_response(response):
        name, op = resolve(response.request)
        status = str(response.status_code)
        if status not in op["responses"]: raise ContractError("status")
        cap = op["responseMaxBytes"][status]
        response_cap=(validate_wire_json_limit(cap)
                      if op["responses"][status] is not None else cap)
        chunks=[]; size=0
        try:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if response_cap is None or size > response_cap:
                    response.close(); raise ContractError("body_limit")
                chunks.append(chunk)
        except ContractError: raise
        raw=b"".join(chunks); response._content=raw
        if op["responses"][status] is None:
            value=ABSENT
        else:
            value=parse_wire_json(raw,max_bytes=response_cap)
        bundle.validate_response(name,response.status_code,value)
        slot=current.get()
        if slot is not None:
            if slot["operation"]!=name: raise ContractError("operation")
            slot["envelopes"].append(Envelope(response.status_code,value,raw))
    transport.event_hooks["request"].append(on_request)
    transport.event_hooks["response"].append(on_response)
    class Guarded:
        def _call(self, name, kwargs):
            op=ops[name]
            binding=native_bindings[name]
            allowed={p["name"] for p in op["parameters"]}
            if op["request"] is not None: allowed.add("body")
            if any(key not in allowed for key in kwargs): raise ContractError("operation")
            for parameter in op["parameters"]:
                value=kwargs.get(parameter["name"], ABSENT)
                if value is ABSENT:
                    if parameter["required"]: raise ContractError("parameter")
                else: bundle.validate_parameter(name,parameter["in"],parameter["name"],value)
            body=kwargs.pop("body", ABSENT)
            if op["request"] is None:
                if body is not ABSENT: raise ContractError("schema_value")
            else: bundle.validate_request(name, body)
            native_kwargs={binding["parameters"].get(p["in"],{}).get(p["name"],p["name"]):kwargs[p["name"]] for p in op["parameters"] if p["name"] in kwargs}
            if body is not ABSENT:
                model_info=binding["model"]
                model=getattr(import_module(".models."+model_info["module"], package=__package__),model_info["class"])
                native_kwargs[binding["body_parameter"]]=model.from_dict(body)
            module=import_module("."+binding["module"], package=__package__)
            slot={"operation":name,"parameters":dict(kwargs),"envelopes":[]}; token=current.set(slot)
            try: module.sync_detailed(client=client, **native_kwargs)
            except ContractError: raise
            except (TypeError,KeyError,AttributeError): raise ContractError("operation") from None
            finally: current.reset(token)
            if not slot["envelopes"]: raise ContractError("status")
            return slot["envelopes"][-1]
    guarded=Guarded()
    def make_method(name):
        def method(**kwargs): return guarded._call(name,kwargs)
        return method
    for name,binding in native_bindings.items(): setattr(guarded,binding["module"].split(".")[-1],make_method(name))
    return guarded
