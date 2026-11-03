"""Task4 admission tests; uses Terraform's native mocked plan, never cloud IO."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import azure_budget as budget
import azure_dns_scope as scope
from dns_proposal import proposal
from azure_teardown import validate as teardown

SUB='11111111-1111-1111-1111-111111111111'
class DNSControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan=json.loads((Path(__file__).parent/'fixtures/dns-native-plan.json').read_text())
        cls.now=dt.datetime(2026,10,24,6,30,tzinfo=dt.timezone.utc)
    def setUp(self):
        self.plan=copy.deepcopy(self.plan)
        self.env=patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB);self.env.start();self.addCleanup(self.env.stop)
    def check(self,plan=None,ledger=None):
        p=plan or self.plan
        rows=proposal(p,'2026-10-24T06:00:00Z')
        return budget.check(p,rows,ledger or {'resources':[]},self.now)
    def test_native_plan_admitted_with_monthly_endpoints_and_day_zone(self):
        rows=self.check()
        endpoints=[r for r in rows if r['type']=='azurerm_traffic_manager_azure_endpoint']
        self.assertEqual(len(endpoints),2)
        self.assertTrue(all(r['estimate_usd']=='0.36' for r in endpoints))
        self.assertEqual(next(r for r in rows if r['type']=='azurerm_private_dns_zone')['bounds']['zone_month'],'0.03571428571428571428571428571')
    def resource(self,typ):
        return next(r for r in self.plan['resource_changes'] if r['type']==typ and r['mode']=='managed' and not r['address'].endswith('-deny"]'))
    def test_foreign_scope_sku_region_and_addons_rejected(self):
        for typ,field,bad in [('azurerm_linux_virtual_machine','size','Standard_D2s_v5'),('azurerm_public_ip','location','francecentral'),('azurerm_traffic_manager_profile','traffic_view_enabled',True),('azurerm_private_dns_zone','name','foreign.internal'),('azurerm_network_security_rule','source_address_prefix','*')]:
            with self.subTest(typ=typ,field=field):
                original=copy.deepcopy(self.plan);self.resource(typ)['change']['after'][field]=bad
                with self.assertRaises(ValueError):self.check()
                self.plan=original
    def test_missing_resource_unknown_input_and_update_rejected(self):
        for mutation in ('missing','unknown','update'):
            with self.subTest(mutation=mutation):
                original=copy.deepcopy(self.plan);r=self.resource('azurerm_linux_virtual_machine')
                if mutation=='missing':self.plan['resource_changes'].remove(r)
                elif mutation=='unknown':r['change']['after']['network_interface_ids']=None;r['change']['after_unknown']['network_interface_ids']=True
                else:r['change']['actions']=['update']
                with self.assertRaises(ValueError):self.check()
                self.plan=original
    def test_prices_quantities_ttl_and_global_meter_mapping_fail_closed(self):
        for kind in ('monthly','region','bounds','ttl','cap'):
            rows=proposal(self.plan,'2026-10-24T06:00:00Z');r=next(r for r in rows['resources'] if r['type']=='azurerm_traffic_manager_azure_endpoint')
            if kind=='monthly':r['estimate_usd']='0.001'
            if kind=='region':r['meters'][0]['selector']['armRegionName']='westus'
            if kind=='bounds':r['bounds']['endpoint_month']='0.01'
            if kind=='ttl':r['end_utc']='2026-10-26T00:00:00Z'
            with self.subTest(kind=kind),self.assertRaises(ValueError):budget.check(self.plan,rows,{'resources':[{'estimate_usd':'18'}] if kind=='cap' else []},self.now)
    def test_campaign_history_survives_deleted_rows_and_rejects_third(self):
        history=[{'type':'azurerm_linux_virtual_machine','region':'swedencentral','campaign_id':str(i),'estimate_usd':'0.1','status':'deleted'} for i in (1,2)]
        with self.assertRaisesRegex(ValueError,'campaign'):self.check(ledger={'resources':history})
    def test_exact_dns_delete_retains_parent_and_rejects_create(self):
        for r in self.plan['resource_changes']:
            if r['mode']!='managed':continue
            v=r['change']['after'];v['id']=scope.resource_ids(SUB)[r['address']]
            r['change']={'actions':['delete'],'before':v,'after':None}
        teardown(self.plan,SUB)
        self.resource('azurerm_public_ip')['change']['actions']=['create']
        with self.assertRaises(ValueError):teardown(self.plan,SUB)

    def test_unknown_optional_input_and_unreconciled_task3_fail_closed(self):
        resource=self.resource('azurerm_linux_virtual_machine')
        resource['change']['after_unknown']['custom_data']=True
        with self.assertRaises(ValueError):self.check()
        resource['change']['after_unknown'].pop('custom_data')
        with self.assertRaisesRegex(ValueError,'Task3'):self.check(ledger={'resources':[{'address':'module.appliance.vm','status':'applied','estimate_usd':'.1'}]})

    def test_parent_current_data_never_falls_back_to_stale_state(self):
        parent=next(r for r in self.plan['resource_changes'] if r['mode']=='data')
        parent['change']['after']=None
        with self.assertRaises(ValueError):self.check()

    def test_stale_capture_and_duplicate_meter_rejected(self):
        rows=proposal(self.plan,'2026-10-24T06:00:00Z')
        paid=next(r for r in rows['resources'] if r['meters'])
        paid['meters'].append(copy.deepcopy(paid['meters'][0]))
        with self.assertRaises(ValueError):budget.check(self.plan,rows,{'resources':[]},self.now)
        rows=proposal(self.plan,'2026-10-24T06:00:00Z')
        with self.assertRaises(ValueError):budget.check(self.plan,rows,{'resources':[]},self.now+dt.timedelta(days=2))

    def test_repriced_retry_preserves_campaign_window_and_history(self):
        old=proposal(self.plan,'2026-10-24T06:00:00Z')['resources']
        self.check(ledger={'resources':old})
        for row in old:row['start_utc']='2026-10-24T05:59:00Z'
        with self.assertRaisesRegex(ValueError,'window'):self.check(ledger={'resources':old})
