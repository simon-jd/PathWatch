#!/usr/bin/env python3
"""Opt-in owned guest install. Run only after reviewed DNS apply and Task3 cleanup."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import signal
import tarfile
import urllib.request
from dns_config import VERSION,COREDNS_SHA256,corefile,wireguard
from dns_runtime import Runtime
from dns_monitor import Monitor

HTTP_SOURCE='''from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/', '/healthz'):
            self.send_error(404); return
        body=MARKER.encode()
        self.send_response(200);self.send_header('Content-Type','text/plain');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_HEAD(self):
        self.send_response(200 if self.path in ('/', '/healthz') else 404);self.end_headers()
    def log_message(self,*args):pass
HTTPServer(('0.0.0.0',80),Handler).serve_forever()
'''
HTTP_UNIT='''[Unit]
Description=PathWatch owned DNS failover HTTP marker
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /opt/pathwatch-dns/http.py
Restart=on-failure
DynamicUser=yes
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
'''
DNS_UNIT='''[Unit]
Description=PathWatch private zone forwarder
After=network-online.target wg-quick@pw-dns.service
Requires=wg-quick@pw-dns.service
[Service]
ExecStart=/opt/pathwatch-dns/coredns -conf /opt/pathwatch-dns/Corefile
Restart=on-failure
DynamicUser=yes
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
'''
def artifact(root,arch):
    path=root/'.cache/dns-artifacts'/f'coredns_{VERSION}_linux_{arch}.tgz';path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        with urllib.request.urlopen(f'https://github.com/coredns/coredns/releases/download/v{VERSION}/'+path.name,timeout=30) as response:
            data=response.read(32*1024*1024+1)
        if len(data)>32*1024*1024:raise ValueError('Unexpected artifact size')
        if hashlib.sha256(data).hexdigest()!=COREDNS_SHA256[arch]:raise ValueError('Official CoreDNS checksum mismatch')
        path.write_bytes(data)
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=COREDNS_SHA256[arch]:raise ValueError('CoreDNS cache checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
        member=archive.getmember('coredns')
        if not member.isfile() or member.size>100*1024*1024:raise ValueError('Unexpected CoreDNS binary')
        return archive.extractfile(member).read()
def install(runtime,keys,out):
    keys=keys.resolve()
    if not keys.is_relative_to(runtime.root/'.cache'):raise ValueError('WG keys stay private in project cache')
    out.mkdir(parents=True,exist_ok=True)
    with Monitor(runtime,out):
        runtime.attest()
        binary=artifact(runtime.root,'arm64')
        for site in ('a','b'):
            if runtime.run(site,['uname','-m']).strip()!='aarch64':raise ValueError('ARM64 guest required')
            runtime.run(site,['sudo','env','DEBIAN_FRONTEND=noninteractive','bash','-c','apt-get update && apt-get install -y --no-install-recommends wireguard-tools dnsutils python3 ca-certificates'],timeout=900)
            wg=wireguard(site,False,(keys/(site+'.key')).read_text().strip(),(keys/('home-'+site+'.pub')).read_text().strip())
            files={'/opt/pathwatch-dns/coredns':(binary,0o755),'/opt/pathwatch-dns/Corefile':(corefile(site).encode(),0o644),'/opt/pathwatch-dns/http.py':(('MARKER='+repr('pathwatch-dns-'+site+'\n')+'\n'+HTTP_SOURCE).encode(),0o644),'/etc/wireguard/pw-dns.conf':(wg.encode(),0o600),'/etc/systemd/system/pathwatch-dns-http.service':(HTTP_UNIT.encode(),0o644),'/etc/systemd/system/pathwatch-dns-core.service':(DNS_UNIT.encode(),0o644)}
            encoded={name:[base64.b64encode(data).decode(),mode] for name,(data,mode) in files.items()}
            source='import base64,json,pathlib\nfiles=json.loads('+repr(json.dumps(encoded))+')\nfor name,(data,mode) in files.items():\n p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(base64.b64decode(data));p.chmod(mode)\n'
            runtime.run(site,['sudo','python3','-'],timeout=120,input=source)
            runtime.run(site,['sudo','systemctl','daemon-reload'])
            runtime.run(site,['sudo','systemctl','enable','--now','wg-quick@pw-dns.service','pathwatch-dns-core.service','pathwatch-dns-http.service'],timeout=60)
            runtime.run(site,['sudo','systemctl','is-active','--quiet','pathwatch-dns-core.service','pathwatch-dns-http.service'])
def main():
    p=argparse.ArgumentParser();p.add_argument('--inventory',required=True);p.add_argument('--keys',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--confirm-owned-endpoints',action='store_true',required=True);a=p.parse_args()
    def cancel(signum,frame):raise KeyboardInterrupt('DNS setup cancelled')
    signal.signal(signal.SIGTERM,cancel);install(Runtime(a.inventory),a.keys,a.out)
if __name__=='__main__':main()
