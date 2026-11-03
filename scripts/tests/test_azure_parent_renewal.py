"""Parent extension uses real canonical pricing/admission, with isolated fixture files."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import azure_budget as budget

SUB = '11111111-1111-1111-1111-111111111111'
NOW = dt.datetime(2026, 10, 24, 20, tzinfo=dt.timezone.utc)
START = '2026-10-24T19:59:00Z'
OLD = '2026-10-25T03:03:27Z'
END = '2026-11-27T19:59:00Z'
RG = '/subscriptions/'+SUB+'/resourceGroups/rg-pathwatch-lab'
STORAGE = RG+'/providers/Microsoft.Storage/storageAccounts/pwteststate'


def fixture(directory):
    tags = dict(project='pathwatch', owner='codex-pathwatch', ttl=OLD, est_usd_per_hour='0.001')
    resources = {
        'azurerm_resource_group.project': dict(id=RG, name='rg-pathwatch-lab', location='westus', tags=tags),
        'azurerm_storage_account.state': dict(id=STORAGE, name='pwteststate', location='westus', resource_group_name='rg-pathwatch-lab', account_tier='Standard', account_replication_type='LRS', account_kind='StorageV2', min_tls_version='TLS1_2', https_traffic_only_enabled=True, shared_access_key_enabled=False, default_to_oauth_authentication=True, allow_nested_items_to_be_public=False, public_network_access_enabled=True, cross_tenant_replication_enabled=False, network_rules=[dict(default_action='Deny', bypass=['None'], ip_rules=['8.8.8.8'])], tags=tags),
        'azurerm_storage_container.state': dict(id=STORAGE+'/blobServices/default/containers/tfstate', name='tfstate', storage_account_id=STORAGE, container_access_type='private'),
        'azurerm_role_assignment.operator_state': dict(id=STORAGE+'/providers/Microsoft.Authorization/roleAssignments/22222222-2222-2222-2222-222222222222', scope=STORAGE, role_definition_name='Storage Blob Data Contributor', principal_id='33333333-3333-3333-3333-333333333333')}
    plan = dict(variables={k:{'value':v} for k,v in dict(subscription_id=SUB, location='westus', ttl=END, storage_account_name='pwteststate', operator_principal_id='33333333-3333-3333-3333-333333333333').items()}, resource_changes=[])
    ledger = dict(resources=[], reconciliation_block=False)
    rows=[]
    for address, before in resources.items():
        typ=address.split('.')[0]; after=copy.deepcopy(before)
        parent=typ in ('azurerm_resource_group','azurerm_storage_account')
        if parent:after['tags']['ttl']=END
        plan['resource_changes'].append(dict(address=address, type=typ, mode='managed', change=dict(actions=['update'] if parent else ['no-op'],before=copy.deepcopy(before),after=after,after_unknown={})))
        ledger['resources'].append(dict(address=address,type=typ,region='westus',subscription_id=SUB,resource_id=before['id'],start_utc='2026-10-24T03:07:47Z',end_utc=OLD,estimate_usd='0.3' if 'storage_account' in address else '0',status='applied',deleted_utc=None,plan_sha256='a'*64))
        if parent:rows.append(dict(address=address,type=typ,region='westus',subscription_id=SUB,resource_id=before['id'],start_utc=START,end_utc=END,previous_end_utc=OLD,estimate_usd='0.5' if 'storage_account' in address else '0',meters=[],renewal_id='renewal-test-001'))
    row=rows[1]; row['bounds']=dict(storage_gb=1,write_requests=10000,read_requests=10000,other_requests=10000,egress_gb=1)
    for category,name,unit,product,sku in [('storage_gb','Hot LRS Data Stored','1 GB/Month','General Block Blob v2','Hot LRS'),('write_requests','Hot LRS Write Operations','10K','General Block Blob v2','Hot LRS'),('read_requests','Hot Read Operations','10K','General Block Blob v2','Hot LRS'),('other_requests','LRS List and Create Container Operations','10K','General Block Blob v2','Hot LRS'),('egress_gb','Standard Data Transfer Out','1 GB','Rtn Preference: MGN','Standard')]:
        selector=dict(meterId=category,skuId='sku-'+category,armRegionName='westus',unitOfMeasure=unit,productName=product,skuName=sku,meterName=name,tierMinimumUnits=100 if category=='egress_gb' else 0)
        capture=category+'.json'
        (directory/capture).write_text(json.dumps(dict(captured_at=START,url='https://prices.azure.com/api/retail/prices?fixture',response=dict(Items=[dict(selector,currencyCode='USD',type='Consumption',effectiveStartDate='2026-01-01T00:00:00Z',retailPrice=0.001)]))))
        row['meters'].append(dict(category=category,capture=capture,selector=selector))
    return plan,dict(purpose='bootstrap-renewal',resources=rows),ledger


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=budget.ROOT/'.cache');self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.plan,self.proposal,self.ledger=fixture(self.path)
        self.addCleanup(patch.stopall)
        patch.object(budget,'ROOT',self.path).start();patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB).start()
    def check(self):return budget.check(self.plan,self.proposal,self.ledger,NOW)
    def test_priced_exact_parent_extension_is_admitted(self):
        self.assertEqual(len(self.check()),2)
    def test_native_terraform_mock_plan_preserves_provider_defaults(self):
        native=json.loads((Path(__file__).parent/'fixtures/parent-native-plan.json').read_text())
        native['variables']=self.plan['variables']
        self.plan=native
        self.assertEqual(len(self.check()),2)
    def test_freeze_precedes_even_valid_parent_extension(self):
        self.ledger['reconciliation_block']=True
        with self.assertRaisesRegex(ValueError,'reconciliation'):self.check()
    def test_parent_immutable_or_unknown_fields_fail_closed(self):
        original=copy.deepcopy(self.plan)
        for change in ('acl','foreign','missing','extra','unknown','deferred','replace','child','earlier','tags'):
            with self.subTest(change=change):
                self.plan=copy.deepcopy(original);r=self.plan['resource_changes'][1]
                if change=='acl':r['change']['after']['network_rules'][0]['default_action']='Allow'
                if change=='foreign':r['change']['before']['id']=STORAGE.replace('pwteststate','pwforeign')
                if change=='missing':self.plan['resource_changes'].pop()
                if change=='extra':self.plan['resource_changes'].append(dict(address='evil',type='azurerm_linux_virtual_machine',mode='managed',change=dict(actions=['no-op'],before={},after={})))
                if change=='unknown':r['change']['after_unknown']={'tags':True}
                if change=='deferred':self.plan['deferred_changes']=[{'reason':'unknown'}]
                if change=='replace':r['change']['actions']=['delete','create']
                if change=='child':self.plan['resource_changes'][2]['change']['actions']=['update']
                if change=='earlier':r['change']['after']['tags']['ttl']='2026-10-24T21:00:00Z'
                if change=='tags':r['change']['after']['tags']['extra']='unreviewed'
                with self.assertRaises(ValueError):self.check()
    def test_missing_deleted_or_mismatched_identity_history_denied(self):
        original=copy.deepcopy(self.ledger)
        for field,value in [('subscription_id',None),('resource_id','foreign'),('status','deleted'),('end_utc',END),('plan_sha256','invalid')]:
            with self.subTest(field=field):
                self.ledger=copy.deepcopy(original);self.ledger['resources'][1][field]=value
                with self.assertRaises(ValueError):self.check()
    def test_retained_historical_cost_counts_toward_cap(self):
        self.ledger['resources'].append(dict(estimate_usd='17.3',status='deleted'))
        with self.assertRaisesRegex(ValueError,'18 USD'):self.check()
    def test_duplicate_renewal_and_window_reset_denied(self):
        self.ledger['resources'].extend(copy.deepcopy(self.proposal['resources']))
        with self.assertRaises(ValueError):self.check()
    def test_complete_current_noop_requires_active_metadata(self):
        for r in self.plan['resource_changes']:
            r['change']['actions']=['no-op'];r['change']['before']=copy.deepcopy(r['change']['after'])
        self.proposal['resources']=[]
        with self.assertRaises(ValueError):self.check()
    def test_valid_noop_after_renewal_needs_no_additional_reservation(self):
        for row in self.proposal['resources']:
            row.update(status='applied',deleted_utc=None,plan_sha256='b'*64)
        self.ledger['resources'].extend(copy.deepcopy(self.proposal['resources']))
        for resource in self.plan['resource_changes']:
            resource['change']['actions']=['no-op']
            resource['change']['before']=copy.deepcopy(resource['change']['after'])
        self.proposal['resources']=[]
        self.assertEqual(self.check(),[])
    def test_duplicate_storage_meter_denied(self):
        self.proposal['resources'][1]['meters'].append(copy.deepcopy(self.proposal['resources'][1]['meters'][0]))
        with self.assertRaises(ValueError):self.check()
    def test_proposal_builder_validates_and_leaves_ledger_unchanged(self):
        import importlib.util
        path=Path(__file__).resolve().parents[1]/'parent_renewal_proposal.py'
        self.assertTrue(path.is_file(),'missing reviewed renewal proposal builder')
        spec=importlib.util.spec_from_file_location('parent_proposal',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        before=copy.deepcopy(self.ledger)
        pricing={k:self.proposal['resources'][1][k] for k in ('bounds','meters','estimate_usd')}
        result=module.build(self.plan,self.ledger,pricing,START,'renewal-test-001',NOW)
        self.assertEqual(result,self.proposal)
        self.assertEqual(self.ledger,before)
    def test_storage_pricing_stale_and_unbounded_requests_denied(self):
        self.proposal['resources'][1]['bounds']['write_requests']=10001
        with self.assertRaises(ValueError):self.check()
        self.proposal['resources'][1]['bounds']['write_requests']=10000
        capture=self.path/'storage_gb.json';data=json.loads(capture.read_text());data['captured_at']='2026-10-20T00:00:00Z';capture.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'stale'):self.check()
    def test_locked_reserve_check_preserves_prior_history_and_is_idempotent(self):
        ledger_path=self.path/'ledger.json';ledger_path.write_text(json.dumps(self.ledger))
        patch.object(budget,'LEDGER',ledger_path).start()
        real_datetime=dt.datetime
        class FixedDatetime(real_datetime):
            @classmethod
            def now(cls,tz=None):return NOW
        patch.object(budget.dt,'datetime',FixedDatetime).start()
        original=copy.deepcopy(self.ledger['resources']);digest='c'*64
        budget.admit('reserve',self.plan,copy.deepcopy(self.proposal),digest)
        reserved=ledger_path.read_bytes()
        for _ in range(2):budget.admit('check',self.plan,copy.deepcopy(self.proposal),digest)
        self.assertEqual(ledger_path.read_bytes(),reserved)
        self.assertEqual(json.loads(reserved)['resources'][:4],original)
        with self.assertRaisesRegex(ValueError,'already reserved'):
            budget.admit('reserve',self.plan,copy.deepcopy(self.proposal),digest)
        frozen=json.loads(reserved);frozen['reconciliation_block']=True;ledger_path.write_text(json.dumps(frozen))
        with self.assertRaisesRegex(ValueError,'reconciliation'):
            budget.admit('check',self.plan,copy.deepcopy(self.proposal),digest)

if __name__=='__main__':unittest.main()
