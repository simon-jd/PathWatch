"""Opt-in native CoreDNS loopback test; no cloud, Docker or public DNS queries."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dns_config import corefile

@unittest.skipUnless(os.environ.get('PATHWATCH_COREDNS_NATIVE'),'explicit checksum-verified native binary required')
class CoreDNSNative(unittest.TestCase):
    def test_zone_forwarding_and_no_open_recursion(self):
        binary=os.environ['PATHWATCH_COREDNS_NATIVE']
        def free_port():
            with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
        front,back=free_port(),free_port()
        config=corefile('a').replace(':53',':'+str(front)).replace('10.10.0.4 10.255.4.1','127.0.0.1').replace('168.63.129.16','127.0.0.1:'+str(back))
        config+=f'dns.pathwatch.internal:{back} {{\n bind 127.0.0.1\n hosts {{\n  10.20.0.4 b.dns.pathwatch.internal\n }}\n}}\n'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Corefile';path.write_text(config)
            with (Path(tmp)/'log').open('w+') as log:
                p=subprocess.Popen([binary,'-conf',str(path)],stdout=log,stderr=subprocess.STDOUT)
                try:
                    def query(name):
                        q=struct.pack('!6H',123,0x100,1,0,0,0)+b''.join(bytes([len(x)])+x.encode() for x in name.split('.'))+b'\0'+struct.pack('!HH',1,1)
                        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
                            s.settimeout(.3);s.sendto(q,('127.0.0.1',front));return s.recv(4096)
                    for _ in range(20):
                        try:reply=query('b.dns.pathwatch.internal');break
                        except (TimeoutError,ConnectionRefusedError):
                            if p.poll() is not None:log.seek(0);self.fail(log.read())
                            time.sleep(.1)
                    else:self.fail('CoreDNS did not become ready')
                    self.assertEqual(reply[-4:],socket.inet_aton('10.20.0.4'))
                    refused=query('foreign.invalid')
                    self.assertNotEqual(struct.unpack('!H',refused[2:4])[0]&15,0)
                    self.assertEqual(struct.unpack('!H',refused[6:8])[0],0)
                finally:
                    p.terminate();p.wait(timeout=5)
