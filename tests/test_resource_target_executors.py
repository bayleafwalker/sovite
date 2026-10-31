"""Fixed executor protocol tests: local TLS daemon and bounded fake subprocess."""
import asyncio
import datetime
import hashlib
import http.server
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import sys
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from sovite.resource_gateway_store import canonical
from sovite.resource_target_catalog import InstalledCatalog,ResourceTargetError
from sovite.resource_target_executors import FixedTargetExecutors

pytestmark=pytest.mark.skipif(sys.version_info<(3,12),reason='Released owner contract runtime requires Python3.12')


@pytest.fixture
def health_daemon(tmp_path,monkeypatch):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'fixture-health-only')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
        x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(
        now+datetime.timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).add_extension(
        x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('192.168.10.55'))]),critical=False).sign(key,hashes.SHA256())
    ca=tmp_path/'ca.pem';ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM));ca.chmod(0o600)
    private=tmp_path/'server.key';private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));private.chmod(0o600)
    observed=[];connections=[];reply=[200,b'{"error":{"message":"ok"}}']
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.command,self.path,dict(self.headers)))
            self.send_response(reply[0]);self.send_header('Content-Length',str(len(reply[1])))
            self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(reply[1])
        def log_message(self,*_):pass
    server=http.server.HTTPServer(('127.0.0.1',0),Handler)
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(ca,private)
    server.socket=tls.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    from httpcore._backends.anyio import AnyIOBackend
    original=AnyIOBackend.connect_tcp
    async def loopback(self,host,port,**kwargs):
        assert host=='192.168.10.55' and port==8021,'No non-fixture network permitted'
        stream=await original(self,'127.0.0.1',server.server_port,**kwargs)
        socket_info=stream.get_extra_info('socket')
        assert socket_info.getpeername()==('127.0.0.1',server.server_port)
        connections.append(socket_info.getpeername())
        return stream
    monkeypatch.setattr(AnyIOBackend,'connect_tcp',loopback)
    target={'kind':'model-peer-health','target_id':'synthetic-health','origin':'https://192.168.10.55:8021',
        'method':'GET','path':'/healthz','ca_file':str(ca),'ca_sha256':hashlib.sha256(ca.read_bytes()).hexdigest()}
    try:yield target,observed,reply,connections
    finally:server.shutdown();server.server_close();thread.join(2)


def test_actual_tls_health_is_exact_get_no_bearer_and_daemon_only(health_daemon):
    target,observed,reply,connections=health_daemon
    assert observed==[] and connections==[]
    assert asyncio.run(FixedTargetExecutors().health(target)) is True
    assert len(connections)==1 and connections[0][0]=='127.0.0.1'
    assert observed[0][0:2]==('GET','/healthz')
    assert not {'Authorization','Cookie','Proxy-Authorization'}&observed[0][2].keys()
    reply[1]=b'{"error":{"message":"ok"},"model_ready":true}'
    with pytest.raises(Exception):asyncio.run(FixedTargetExecutors().health(target))
    reply[:]=[302,b'{}']
    with pytest.raises(ResourceTargetError):asyncio.run(FixedTargetExecutors().health(target))
    assert len(observed)==len(connections)==3


def test_health_ca_rotation_and_nonfixed_origins_refuse_before_network(health_daemon,tmp_path):
    target,observed,_,connections=health_daemon
    target['ca_sha256']='0'*64
    with pytest.raises(ResourceTargetError):asyncio.run(FixedTargetExecutors().health(target))
    assert observed==[] and connections==[]
    target['origin']='https://other.invalid'
    path=tmp_path/'catalog';path.write_bytes(canonical({'schema_version':'sovite-resource-installed-catalog/v2','entries':[target]}))
    with pytest.raises(ResourceTargetError):InstalledCatalog(path)


def request():
    return {'schema_version':'cloud.kvm-lab-control.v1','verb':'create','operation_id':'a'*64,
        'create_operation_id':'a'*64,'template_id':'diskless-kvm-lab-v1','template_sha256':'f'*64,'deadline':2000000000}


class Input:
    def __init__(self):self.value=b'';self.closed=False
    def write(self,value):self.value+=value
    async def drain(self):pass
    def close(self):self.closed=True


class Process:
    def __init__(self,raw):
        self.stdin=Input();self.stdout=asyncio.StreamReader();self.stdout.feed_data(raw);self.stdout.feed_eof()
        self.returncode=None;self.killed=False
    async def wait(self):self.returncode=0;return 0
    def kill(self):self.killed=True;self.returncode=-9


def test_helper_invocation_fixed_argv_and_exact_boot_unit_tuple(monkeypatch):
    value=request();boot='12345678-1234-1234-1234-123456789abc'
    reply={**{key:item for key,item in value.items() if key!='verb'},'schema_version':'cloud.kvm-lab-observation.v1',
        'state':'running','host_boot_id':boot,'created_boot_id':boot,
        'unit':'cloud-kvm-lab-'+hashlib.sha256(value['create_operation_id'].encode()).hexdigest()+'.service',
        'invocation_id':'b'*32}
    calls=[];processes=[]
    async def spawn(*args,**kwargs):
        calls.append((args,kwargs));process=Process(canonical(reply));processes.append(process);return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    result=asyncio.run(FixedTargetExecutors().lab(value))
    assert result==reply and calls[0][0]==('/run/current-system/sw/bin/cloud-kvm-lab',)
    assert calls[0][1]['env']=={'PATH':'/run/current-system/sw/bin'}
    assert processes[0].stdin.value==canonical(value)+b'\n' and processes[0].stdin.closed
    for key,changed in [('create_operation_id','c'*64),('unit','foreign.service'),('deadline',1999999999),
                        ('created_boot_id',None),('invocation_id',None)]:
        original=reply[key];reply[key]=changed
        with pytest.raises(ResourceTargetError):asyncio.run(FixedTargetExecutors().lab(value))
        reply[key]=original


def test_oversized_helper_response_kills_client_without_retry(monkeypatch):
    processes=[];calls=[]
    async def spawn(*args,**kwargs):
        calls.append(args);process=Process(b'x'*4097);processes.append(process);return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    with pytest.raises(ResourceTargetError):asyncio.run(FixedTargetExecutors().lab(request()))
    assert len(calls)==1 and processes[0].killed
