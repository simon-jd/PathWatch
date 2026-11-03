"""Local-only controls; fake Azure CLI, never live mutations."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('budget', ROOT / 'scripts/azure_budget.py')
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)
class BudgetTests(unittest.TestCase):
    def test_empty_ledger(self):
        self.assertEqual(budget.total_ledger({'resources': []}), 0)
    def test_negative_ledger_rejected(self):
        with self.assertRaises(ValueError): budget.total_ledger({'resources': [{'estimate_usd': -1}]})
    def test_cap(self):
        with self.assertRaises(ValueError): budget.enforce_cap(17, 2)
        budget.enforce_cap(17, 1)
    def test_strict_utc(self):
        for s in ('2026-10-23', '2026-10-23T00:00:00+00:00', '2026-02-30T00:00:00Z'):
            with self.assertRaises(ValueError): budget.utc(s)
    def test_unknown_price(self):
        with self.assertRaises(ValueError): budget.validate_meter({}, {}, dt.datetime.now(dt.timezone.utc))
class PlanBudgetTests(unittest.TestCase):
    def setUp(self):
        self.sub='11111111-1111-1111-1111-111111111111'
        self.env=patch.dict(os.environ, {'AZURE_SUBSCRIPTION_ID': self.sub});self.env.start();self.addCleanup(self.env.stop)
        raw=json.loads((ROOT/'docs/azure-prices/2026-10-23-storage-westus2.json').read_text())
        self.now=budget.utc(raw['captured_at'])+dt.timedelta(seconds=1)
        self.start=(self.now-dt.timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.end=(self.now+dt.timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
        tags={'project':'pathwatch','owner':'codex-pathwatch','ttl':self.end,'est_usd_per_hour':'0.01'}
        self.plan={'variables': {'subscription_id':{'value':self.sub},'location':{'value':'westus2'}},'resource_changes':[{'mode':'managed','address':'azurerm_storage_account.state','type':'azurerm_storage_account','change':{'actions':['create'],'after':{'resource_group_name':'rg-pathwatch-lab','account_tier':'Standard','account_replication_type':'LRS','tags':tags}}}]}
        meters=[]
        for category,name in {'storage_gb':'Hot LRS Data Stored','write_requests':'Hot LRS Write Operations','read_requests':'Hot Read Operations','other_requests':'LRS List and Create Container Operations','egress_gb':'Standard Data Transfer Out'}.items():
            capture='docs/azure-prices/2026-10-23-'+('bandwidth' if category=='egress_gb' else 'storage')+'-westus2.json'
            data=json.loads((ROOT/capture).read_text())
            item=next(i for i in data['response']['Items'] if i['meterName']==name and i['tierMinimumUnits']==(100 if category=='egress_gb' else 0) and (category!='egress_gb' or i['productName']=='Rtn Preference: MGN'))
            selector={k:item[k] for k in ('meterId','skuId','armRegionName','unitOfMeasure','productName','skuName','meterName','tierMinimumUnits')}
            meters.append({'category':category,'capture':capture,'selector':selector})
        self.proposal={'resources':[{'address':'azurerm_storage_account.state','type':'azurerm_storage_account','region':'westus2','start_utc':self.start,'end_utc':self.end,'estimate_usd':0.3,'bounds':{'storage_gb':1,'write_requests':10000,'read_requests':10000,'other_requests':10000,'egress_gb':1},'meters':meters}]}
        self.now=max(self.now,budget.utc(json.loads((ROOT/'docs/azure-prices/2026-10-23-bandwidth-westus2.json').read_text())['captured_at']))+dt.timedelta(seconds=1)
    def check(self):return budget.check(self.plan,self.proposal,{'resources':[]},self.now)
    def test_valid_bounded_real_capture(self):self.check()
    def test_missing_entry(self):
        self.proposal['resources']=[]
        with self.assertRaises(ValueError):self.check()
    def test_protected_scope(self):
        for key in ('resource_group_name','scope','parent_id'):
            plan=copy.deepcopy(self.plan)
            self.plan['resource_changes'][0]['change']['after'][key]='rg-mlinfra-westus' if key=='resource_group_name' else '/subscriptions/'+self.sub+'/resourceGroups/rg-mlinfra-westus/providers/X/y'
            with self.assertRaises(ValueError):self.check()
            self.plan=plan
    def test_missing_tags(self):
        for key in ('ttl','owner','est_usd_per_hour'):
            plan=copy.deepcopy(self.plan);del self.plan['resource_changes'][0]['change']['after']['tags'][key]
            with self.assertRaises(ValueError):self.check()
            self.plan=plan
    def test_updates_and_deletes_refused(self):
        for actions in (['update'],['delete'],['delete','create']):
            self.plan['resource_changes'][0]['change']['actions']=actions
            with self.assertRaises(ValueError):self.check()
    def test_noop_does_not_need_new_reservation(self):
        self.plan['resource_changes'][0]['change']['actions']=['no-op'];self.proposal['resources']=[];self.check()
    def test_stale_capture(self):
        raw=json.loads((ROOT/'docs/azure-prices/2026-10-23-storage-westus2.json').read_text())
        sel=self.proposal['resources'][0]['meters'][0]['selector']
        with self.assertRaises(ValueError):budget.validate_meter(raw,sel,self.now+dt.timedelta(days=2))
        with self.assertRaises(ValueError):budget.validate_meter(raw,sel,self.now-dt.timedelta(days=1))
    def test_unit_and_sku_mismatch(self):
        for key in ('unitOfMeasure','skuName','meterId','armRegionName'):
            prop=copy.deepcopy(self.proposal);self.proposal['resources'][0]['meters'][0]['selector'][key]='wrong'
            with self.assertRaises(ValueError):self.check()
            self.proposal=prop
    def test_nan_negative_or_infinite_prices(self):
        raw=json.loads((ROOT/'docs/azure-prices/2026-10-23-storage-westus2.json').read_text())
        sel=self.proposal['resources'][0]['meters'][0]['selector']
        for price in ('NaN',-1,'Infinity'):
            evidence=copy.deepcopy(raw)
            for item in evidence['response']['Items']:
                if item['meterId']==sel['meterId']:item['retailPrice']=price
            with self.assertRaises(ValueError):budget.validate_meter(evidence,sel,self.now)
    def test_budget_prior_spend_retained_after_deletion(self):
        with self.assertRaises(ValueError):budget.check(self.plan,self.proposal,{'resources':[{'estimate_usd':17.9,'status':'deleted'}]},self.now)
    def test_storage_requests_bounded(self):
        self.proposal['resources'][0]['bounds']['write_requests']=10001
        with self.assertRaises(ValueError):self.check()
    def test_unpriced_meter_refused(self):
        self.proposal['resources'][0]['meters'].pop()
        with self.assertRaises(ValueError):self.check()
    def test_underestimated_cost(self):
        self.proposal['resources'][0]['estimate_usd']=0.001
        with self.assertRaises(ValueError):self.check()
class ReaperTests(unittest.TestCase):
    def run_reaper(self, resources, fail='', group='true', mode='reap'):
        with tempfile.TemporaryDirectory(dir=ROOT / '.cache') as td:
            td=Path(td); log=td/'calls'
            fake=td/'az'; fake.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps(args)+'\\n')
if os.environ.get('FAIL') and args[:2]==os.environ['FAIL'].split():sys.exit(9)
if args[:2]==['account','show']:print(json.dumps({'id':os.environ['AZURE_SUBSCRIPTION_ID'],'name':'Azure for Students','state':'Enabled'}))
elif args[:2]==['group','exists']:print(os.environ['GROUP'])
elif args[:2]==['resource','list']:print(os.environ['RESOURCES'])
elif args[:2]==['group','show']:print(json.dumps({'tags': {'project':'pathwatch','owner':'codex-pathwatch'}}))
''');fake.chmod(0o755)
            env=os.environ|{'PATH':str(td)+':'+os.environ['PATH'],'AZURE_SUBSCRIPTION_ID':'11111111-1111-1111-1111-111111111111','CALLS':str(log),'RESOURCES':json.dumps(resources),'FAIL':fail,'GROUP':group}
            r=subprocess.run(['bash',str(ROOT/'scripts/azure_reaper.sh'),mode],env=env,text=True,capture_output=True)
            calls=[json.loads(s) for s in log.read_text().splitlines()] if log.exists() else []
            return r,calls
    def resource(self, ttl='2000-01-01T00:00:00Z', **kw):
        r={'id':'/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Storage/storageAccounts/pwtest','tags':{'project':'pathwatch','owner':'codex-pathwatch','ttl':ttl,'est_usd_per_hour':'0.001'}}
        r.update(kw);return r
    def test_expired_only(self):
        r,c=self.run_reaper([self.resource(), self.resource('2999-01-01T00:00:00Z')]);self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(sum(x[:2]==['resource','delete'] for x in c),1)
    def test_unsafe_never_deleted(self):
        items=[self.resource('invalid'),self.resource('2020-01-01T00:00:00+00:00'),self.resource(tags={}),self.resource(id='/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-mlinfra-westus/providers/Microsoft.Storage/storageAccounts/test'),self.resource(id='/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg-pathwatch-lab/providers/x/y')]
        r,c=self.run_reaper(items);self.assertFalse(any(x[:2]==['resource','delete'] for x in c))
    def test_missing_group(self):
        r,c=self.run_reaper([],group='false');self.assertEqual(r.returncode,0);self.assertFalse(any(x[:2]==['resource','list'] for x in c))
    def test_account_and_group_errors_fail_closed(self):
        for command in ('account show','group exists'):
            r,c=self.run_reaper([],fail=command);self.assertNotEqual(r.returncode,0)
            self.assertFalse(any(x[:2]==['resource','delete'] for x in c))
    def test_list_failure(self):
        r,c=self.run_reaper([],fail='resource list');self.assertNotEqual(r.returncode,0)
    def test_delete_failure_not_hidden(self):
        r,c=self.run_reaper([self.resource()],fail='resource delete');self.assertNotEqual(r.returncode,0)
    def test_down_is_exact_group(self):
        r,c=self.run_reaper([],mode='down');self.assertEqual(r.returncode,0,r.stderr)
        deletes=[x for x in c if x[:2]==['group','delete']];self.assertEqual(len(deletes),1)
        self.assertIn('rg-pathwatch-lab',deletes[0]);self.assertIn('--subscription',deletes[0])
if __name__=='__main__':unittest.main()
