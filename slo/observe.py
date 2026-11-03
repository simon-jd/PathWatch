#!/usr/bin/env python3
"""Independent 15s readiness requests and immutable closed UTC 5m windows.

A new roster requires a new observation epoch/state file. This process must live
outside ingest's failure domain. Persistent gaps are bad, never zero traffic.
"""
import argparse
import http.server
import ipaddress
import json
import os
import pathlib
import re
import threading
import time
import urllib.request
import urllib.parse

STEP = 15

def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

class Observer:
    def __init__(self, path, roster, now):
        if not isinstance(roster, dict) or not 1 <= len(roster) <= 32 or len(set(roster.values())) != len(roster):
            raise ValueError('1..32 distinct physical sites required; local processes are not sites')
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', x) for pair in roster.items() for x in pair):
            raise ValueError('invalid roster identity')
        self.path, self.roster = pathlib.Path(path), roster
        self.required = max(1, len(roster)-1)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state['roster'] != roster or self.state['version'] != 1:
                raise ValueError('roster/state epoch mismatch')
        else:
            self.state = dict(version=1, roster=roster, start=int(now), last_slot=int(now)//STEP,
                              window=int(now)//300*300, fresh=[], windows_total=0, windows_bad=0, last_window_bad=1,
                              api_total=0, api_bad=0, last_success=0, journal_records=0, journal_bytes=0)
            atomic_json(self.path,self.state)
        self.lock = threading.Lock()

    def tick(self, now, response):
        now, slot = int(now), int(now)//STEP
        with self.lock:
            s=self.state
            if slot <= s['last_slot']: return
            elapsed = slot-s['last_slot']
            s['api_total'] += elapsed; s['api_bad'] += elapsed-1
            # Finalize BEFORE reading this response: late data never revises history.
            while s['window']+300 <= now:
                s['windows_total'] += 1
                s['last_window_bad'] = int(len(s['fresh']) < self.required)
                s['windows_bad'] += s['last_window_bad']
                s['window'] += 300; s['fresh'] = []
            good = isinstance(response,dict) and isinstance(response.get('windows'),dict)
            if good:
                for key, ids in response['windows'].items():
                    if not key.isdigit() or int(key)%300 or not isinstance(ids,list) or any(not isinstance(x,str) or x not in self.roster for x in ids):
                        good=False;break
            if good:
                s['fresh']=sorted(set(s['fresh']) | set(response['windows'].get(str(s['window']),[])))
                s['last_success']=now
                for key in ('journal_records','journal_bytes'): s[key]=max(0,int(response.get(key,0)))
            else: s['api_bad'] += 1
            s['last_slot']=slot
            atomic_json(self.path,s)

    def metrics(self):
        with self.lock:
            s=self.state
            rows = {f'pathwatch_slo_{k}':s[k] for k in ('windows_total','windows_bad','api_total','api_bad')}
            rows.update(pathwatch_slo_expected_observers=len(self.roster), pathwatch_slo_required_observers=self.required,
                        pathwatch_slo_last_observation_seconds=s['last_slot']*STEP,
                        pathwatch_slo_observation_start_seconds=s['start'],
                        pathwatch_slo_last_window_bad=s['last_window_bad'],
                        pathwatch_slo_current_fresh_observers=len(s['fresh']),
                        pathwatch_journal_records=s['journal_records'],pathwatch_journal_bytes=s['journal_bytes'])
            return ''.join(f'{k} {v}\n' for k,v in rows.items())

def main():
    p=argparse.ArgumentParser();p.add_argument('--roster',required=True);p.add_argument('--state',required=True)
    p.add_argument('--endpoint',default='http://127.0.0.1:9091/readyz');p.add_argument('--port',type=int,default=9465)
    a=p.parse_args();u=urllib.parse.urlsplit(a.endpoint)
    if u.scheme!='http' or not ipaddress.ip_address(u.hostname).is_loopback or u.path!='/readyz' or u.username or u.query or u.fragment:
        p.error('owned literal-loopback /readyz required')
    # Acquire ownership before opening or creating history.
    import fcntl
    pathlib.Path(a.state).parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=open(str(a.state)+'.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    o=Observer(a.state,json.loads(pathlib.Path(a.roster).read_text()),time.time())
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/metrics':self.send_error(404);return
            data=o.metrics().encode();self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',a.port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        time.sleep(max(.1,STEP-time.time()%STEP))
        response=None
        try:
            with opener.open(a.endpoint,timeout=3) as r:
                if r.status==200:response=json.loads(r.read(65537))
        except (OSError,ValueError):pass
        o.tick(time.time(),response)
if __name__=='__main__':main()
