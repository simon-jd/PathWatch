"""I1/I2/I3 regressions: local fixtures and mocked process boundaries only."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import azure_budget as budget
import azure_dns_scope as scope
from dns_proposal import proposal
from dns_runtime import Runtime
from dns_monitor import Monitor
from dns_failover import campaign

ROOT=Path(__file__).resolve().parents[2]
SUB='11111111-1111-1111-1111-111111111111'
NOW=dt.datetime(2026,10,24,6,30,tzinfo=dt.timezone.utc)
START='2026-10-24T06:00:00Z'

class DNSFix1(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads((ROOT/'scripts/tests/fixtures/dns-native-plan.json').read_text())
        self.rows=proposal(self.plan,START)['resources']
        for r in self.rows:r.update(status='applied',plan_sha256='a'*64,deleted_utc=None)
        self.ledger={'resources':self.rows}
        self.env=patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB);self.env.start();self.addCleanup(self.env.stop)
    def noop(self):
        for r in self.plan['resource_changes']:
            if r.get('mode')=='managed':r['change']['actions']=['no-op']
    def test_I1_noop_campaign_and_expiry_bound_to_active_reservation(self):
        self.noop()
        self.assertEqual(budget.check(self.plan,{'resources':[]},self.ledger,NOW),[])
        for mutation in ('new_uuid','expired','ended','missing','foreign_active','window','query_bounds','subscription','unreserved'):
            p=copy.deepcopy(self.plan);l=copy.deepcopy(self.ledger);now=NOW
            if mutation=='new_uuid':p['variables']['campaign_id']['value']='33333333-3333-3333-3333-333333333333'
            elif mutation=='expired':now=NOW+dt.timedelta(hours=3)
            elif mutation=='ended':
                for r in l['resources']:r['status']='deleted';r['deleted_utc']=START
            elif mutation=='missing':l['resources'].pop()
            elif mutation=='foreign_active':l['resources'][0]['campaign_id']='33333333-3333-3333-3333-333333333333'
            elif mutation=='window':l['resources'][0]['start_utc']='2026-10-24T05:59:00Z'
            elif mutation=='query_bounds':next(r for r in l['resources'] if r['type']=='azurerm_traffic_manager_profile')['bounds']['public_queries']='1'
            elif mutation=='subscription':l['resources'][0]['subscription_id']='22222222-2222-2222-2222-222222222222'
            else:l['resources'][0].pop('plan_sha256')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):budget.check(p,{'resources':[]},l,now)
    def test_I1_runtime_inventory_must_bind_reserved_uuid_and_ttl(self):
        class Clock(dt.datetime):
            @classmethod
            def now(cls,tz=None):return NOW
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as tmp:
            base=Path(tmp);key=base/'key';key.write_text('test');key.chmod(0o600);known=base/'known';known.write_text('test')
            path=base/'inventory.json';ledger=base/'ledger.json';ledger.write_text(json.dumps(self.ledger))
            inventory={'scope':'pathwatch-dns-owned','subscription_id':SUB,'task3_cleanup_confirmed':True,'campaign_id':self.plan['variables']['campaign_id']['value'],'ttl':self.plan['variables']['ttl']['value'],'owner_cidr':'9.9.9.9/32','zone':scope.ZONE,'tm_name':scope.label(SUB),'tm_fqdn':scope.label(SUB)+'.trafficmanager.net','ssh_private_key':str(key),'known_hosts':str(known),'sites':{s:{'public_ip':ip,'private_ip':scope.IPS[s],'region':scope.REGIONS[s],'vm_id':scope.resource_ids(SUB)[scope.address('azurerm_linux_virtual_machine',s)]} for s,ip in [('a','8.8.8.8'),('b','1.1.1.1')]}}
            with patch.object(budget,'LEDGER',ledger),patch('dns_runtime.dt.datetime',Clock):
                path.write_text(json.dumps(inventory));Runtime(path)
                for field,value in [('campaign_id','33333333-3333-3333-3333-333333333333'),('ttl','2026-10-24T09:00:00Z')]:
                    bad={**inventory,field:value};path.write_text(json.dumps(bad))
                    with self.subTest(field=field),self.assertRaises(ValueError):Runtime(path)
                path.write_text(json.dumps(inventory))
                with patch.dict(os.environ,AZURE_SUBSCRIPTION_ID='22222222-2222-2222-2222-222222222222'),self.assertRaises(ValueError):Runtime(path)
                for row in self.rows:row['status']='deleted';row['deleted_utc']=START
                ledger.write_text(json.dumps(self.ledger))
                with self.assertRaises(ValueError):Runtime(path)

    def test_I2_terminal_guest_and_monitor_usage_cannot_publish_success(self):
        with self.subTest(boundary='guest'),tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'scripts').mkdir()
            (root/'scripts/hybrid_usage.py').write_text("count=0\ndef snapshot():\n global count\n count+=1\n return 0 if count==1 else 800000\ndef enforce(sample,egress_gb,transactions):\n if sample>=transactions*.8:raise ValueError('80% stop')\n")
            r=Runtime.__new__(Runtime);r.root=root;r.key=Path('/fake');r.known=Path('/fake');r.config={'ttl':'2100-01-01T00:00:00Z','sites':{'a':{'public_ip':'8.8.8.8'}}}
            with patch('dns_runtime.subprocess.run',return_value=MagicMock(stdout='')) as run:r.run('a',['true'])
            code=shlex.split(run.call_args.args[0][-1])[-1]
            process=MagicMock(returncode=0);process.poll.return_value=0
            previous={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP)}
            try:
                with patch('subprocess.Popen',return_value=process),self.assertRaisesRegex(ValueError,'80%'):exec(code,{})
                process.returncode=7
                with patch('subprocess.Popen',return_value=process),self.assertRaises(SystemExit) as failure:exec(code,{})
                self.assertEqual(failure.exception.code,7)
            finally:
                for sig,handler in previous.items():signal.signal(sig,handler)
        with self.subTest(boundary='controller'),tempfile.TemporaryDirectory() as tmp:
            r=MagicMock();r.expiry=dt.datetime(2100,1,1,tzinfo=dt.timezone.utc)
            sample=lambda n:{'a':{'disks':{'sda':{'bounded_transactions':n}},'tx_bytes':{'eth0':0}}}
            r.usage.side_effect=[sample(0),sample(800000)]
            with self.assertRaisesRegex(ValueError,'80%'):
                with Monitor(r,tmp):pass
            self.assertEqual(r.usage.call_count,2)
    def test_I2_monitor_final_failure_restores_and_suppresses_campaign_summary(self):
        runtime=MagicMock();runtime.expiry=dt.datetime(2100,1,1,tzinfo=dt.timezone.utc)
        runtime.config={'sites':{'a':{'public_ip':'8.8.8.8'},'b':{'public_ip':'1.1.1.1'}}}
        runtime.observe.side_effect=[{'site':s,'monotonic_ns':1,'answers':['8.8.8.8']} for s in ('home','a','b')]+[{'site':s,'monotonic_ns':61000000001,'answers':['1.1.1.1']} for s in ('home','a','b')]
        sample=lambda n:{'a':{'disks':{'sda':{'bounded_transactions':n}},'tx_bytes':{'eth0':0}}}
        runtime.usage.side_effect=[sample(0),sample(800000)]
        with tempfile.TemporaryDirectory() as tmp:
            with patch('dns_failover.time.sleep'),self.assertRaisesRegex(ValueError,'80%'):campaign(runtime,tmp,rounds=1)
            self.assertEqual(runtime.service.call_args_list[-1].args,('start',))
            run=next(Path(tmp).iterdir());self.assertFalse((run/'summary.json').exists())
            self.assertEqual(json.loads((run/'run.json').read_text())['status'],'failed')

    def test_I3_actual_unconfigured_optional_unknown_shapes_accepted(self):
        for r in self.plan['resource_changes']:
            key={'azurerm_linux_virtual_machine':'termination_notification','azurerm_private_dns_zone':'soa_record'}.get(r['type'])
            if key:r['change']['after'][key]=None;r['change']['after_unknown'][key]=True
        scope.validate(self.plan,SUB)
        configs=self.plan['configuration']['root_module']['module_calls']['dns']['module']['resources']
        for typ,key in [('azurerm_linux_virtual_machine','termination_notification'),('azurerm_private_dns_zone','soa_record')]:
            for mutation in ('explicit','unknown_expression','missing_config','nested_unknown','known_addon'):
                p=copy.deepcopy(self.plan);cs=p['configuration']['root_module']['module_calls']['dns']['module']['resources'];c=next(c for c in cs if c['address']==typ+'.this')
                r=next(r for r in p['resource_changes'] if r['type']==typ)
                if mutation=='explicit':c['expressions'][key]={'constant_value':[{}]}
                elif mutation=='unknown_expression':c['expressions'][key]={'references':['var.external']}
                elif mutation=='missing_config':cs.remove(c)
                elif mutation=='nested_unknown':r['change']['after_unknown'][key]=[{'enabled':True}]
                else:r['change']['after_unknown'].pop(key);r['change']['after'][key]=[{'enabled':True}] if key=='termination_notification' else [{'email':'foreign.example'}]
                with self.subTest(typ=typ,mutation=mutation),self.assertRaises(ValueError):scope.validate(p,SUB)

    def test_I3_actual_regular_vm_default_bid_sentinel(self):
        for r in self.plan['resource_changes']:
            if r['type']=='azurerm_linux_virtual_machine':r['change']['after']['max_bid_price']=-1
        scope.validate(self.plan,SUB)
        for mutation in ('explicit','positive','unknown'):
            p=copy.deepcopy(self.plan)
            if mutation=='explicit':
                cs=p['configuration']['root_module']['module_calls']['dns']['module']['resources'];next(c for c in cs if c['address']=='azurerm_linux_virtual_machine.this')['expressions']['max_bid_price']={'constant_value':-1}
            else:
                r=next(r for r in p['resource_changes'] if r['type']=='azurerm_linux_virtual_machine')
                if mutation=='positive':r['change']['after']['max_bid_price']=1
                else:r['change']['after']['max_bid_price']=None;r['change']['after_unknown']['max_bid_price']=True
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):scope.validate(p,SUB)

    def test_I3_known_defaults_noop_delete_and_explicit_addons(self):
        soa={'email':'azureprivatedns-host.microsoft.com','host_name':'azureprivatedns.net.','expire_time':2419200,'minimum_ttl':10,'refresh_time':3600,'retry_time':300,'ttl':3600,'tags':{},'fqdn':scope.ZONE+'.','serial_number':1}
        for r in self.plan['resource_changes']:
            if r.get('mode')!='managed':continue
            typ=r['type'];v=r['change']['after'];v['id']=scope.resource_ids(SUB)[r['address']]
            if typ=='azurerm_linux_virtual_machine':v['termination_notification']=[{'enabled':False,'timeout':'PT5M'}]
            if typ=='azurerm_private_dns_zone':v['soa_record']=[copy.deepcopy(soa)]
            r['change']={'actions':['no-op'],'before':copy.deepcopy(v),'after':v,'after_unknown':{}}
        scope.validate(self.plan,SUB)
        for deleting in (False,True):
            p=copy.deepcopy(self.plan)
            if deleting:
                for r in p['resource_changes']:
                    if r.get('mode')=='managed':r['change']['actions']=['delete'];r['change']['after']=None
            scope.validate(p,SUB,deleting=deleting)
            for typ,key,field,value in [('azurerm_linux_virtual_machine','termination_notification','enabled',True),('azurerm_private_dns_zone','soa_record','email','foreign.example'),('azurerm_private_dns_zone','soa_record','ttl',60),('azurerm_private_dns_zone','soa_record','tags',{'owner':'foreign'})]:
                bad=copy.deepcopy(p);r=next(r for r in bad['resource_changes'] if r['type']==typ)
                r['change']['before' if deleting else 'after'][key][0][field]=value
                with self.subTest(deleting=deleting,field=field),self.assertRaises(ValueError):scope.validate(bad,SUB,deleting=deleting)
            bad=copy.deepcopy(p);cs=bad['configuration']['root_module']['module_calls']['dns']['module']['resources'];next(c for c in cs if c['address']=='azurerm_private_dns_zone.this')['expressions']['soa_record']={'constant_value':[soa]}
            with self.assertRaises(ValueError):scope.validate(bad,SUB,deleting=deleting)

if __name__=='__main__':unittest.main()
