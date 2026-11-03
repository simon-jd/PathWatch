#!/usr/bin/env python3
"""10-minute raw-data presence diagnostic, separate from committed-arrival SLO."""
import argparse,base64,ipaddress,json,os,pathlib,re,ssl,urllib.parse,urllib.request

def validate_url(url):
    u=urllib.parse.urlsplit(url)
    if u.scheme not in ('http','https') or not u.hostname or not u.port or u.username or u.query or u.fragment or u.path not in ('','/'):
        raise ValueError('explicit http(s) host:port base URL required')
    if u.scheme=='http':
        try: local=ipaddress.ip_address(u.hostname).is_loopback
        except ValueError: local=False
        if not local:raise ValueError('plaintext only for literal loopback')
    return u

def check_result(count, ids, roster):
    if not isinstance(roster,dict) or not 1<=len(roster)<=32 or len(set(roster.values()))!=len(roster):
        raise ValueError('roster must name unique physical sites')
    if count!=len(roster) or len(ids)!=count or set(ids)!=set(roster):
        raise ValueError(f'10-minute observers mismatch: count={count} ids={ids} expected={sorted(roster)}')

def main():
    p=argparse.ArgumentParser();p.add_argument('--roster',required=True);p.add_argument('--url',default='http://127.0.0.1:8123');p.add_argument('--database',default='pathwatch');p.add_argument('--ca');a=p.parse_args()
    validate_url(a.url)
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',a.database):p.error('invalid database')
    ctx=ssl.create_default_context(cafile=a.ca)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ctx))
    user=os.environ.get('PATHWATCH_CLICKHOUSE_USER','pathwatch_ingest');password=os.environ.get('PATHWATCH_CLICKHOUSE_PASSWORD','')
    def query(sql):
        u=a.url.rstrip('/')+'/?'+urllib.parse.urlencode({'database':a.database,'max_execution_time':'5','max_memory_usage':'67108864','max_threads':'1'})
        r=urllib.request.Request(u,data=sql.encode(),headers={'Authorization':'Basic '+base64.b64encode((user+':'+password).encode()).decode()})
        with opener.open(r,timeout=7) as response:
            data=response.read(65537)
            if len(data)>65536:raise ValueError('response exceeded bound')
            return data.decode().strip()
    count=int(query('SELECT uniqExact(probe_id) FROM probe_results WHERE started_at > now() - INTERVAL 10 MINUTE'))
    ids=json.loads(query('SELECT arraySort(groupUniqArray(probe_id)) FROM probe_results WHERE started_at > now() - INTERVAL 10 MINUTE FORMAT JSONEachRow'))['arraySort(groupUniqArray(probe_id))']
    check_result(count,ids,json.loads(pathlib.Path(a.roster).read_text()))
    print(json.dumps({'observed':count,'probe_ids':ids,'range_minutes':10,'scope':'raw-presence-only; not on-time SLO'}))
if __name__=='__main__':main()
