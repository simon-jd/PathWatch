#!/usr/bin/env python3
"""One DNS observation, executed on each physical observer. No internal retries."""
import ipaddress
import json
import re
import subprocess
import sys
import time

def query(site,target):
    if site not in ('home','a','b') or not re.fullmatch(r'pathwatch-[0-9a-f]{10}-dns\.trafficmanager\.net',target):raise ValueError('Only owned TM target')
    before=time.monotonic_ns();wall=time.time_ns()
    p=subprocess.run(['dig','+tries=1','+time=3','+noall','+answer',target,'A'],text=True,capture_output=True,timeout=5,check=True)
    answers=[]
    for line in p.stdout.splitlines():
        fields=line.split()
        if len(fields)>=5 and fields[3]=='A':answers.append(str(ipaddress.IPv4Address(fields[4])))
    return {'site':site,'target':target,'monotonic_ns':before,'realtime_ns':wall,'elapsed_ns':time.monotonic_ns()-before,'answers':sorted(set(answers)),'raw_answer':p.stdout}
if __name__=='__main__':print(json.dumps(query(*sys.argv[1:])))
