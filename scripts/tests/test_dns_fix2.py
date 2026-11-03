"""Actual Azure compatibility contracts; no resource or canonical ledger writes."""
import copy
import datetime as dt
import os
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import azure_dns_scope as scope
import azure_budget as budget
from dns_proposal import proposal
from azure_teardown import validate as teardown

SUB='11111111-1111-1111-1111-111111111111'
class DNSFix2(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads((Path(__file__).parent/'fixtures/dns-native-plan.json').read_text())
    def config(self,plan,typ):
        cs=plan['configuration']['root_module']['module_calls']['dns']['module']['resources']
        return next(c['expressions'] for c in cs if c['address']==typ+'.this')
    def links(self,plan):return [r for r in plan['resource_changes'] if r['type']=='azurerm_private_dns_zone_virtual_network_link']
    def partial_delete(self):
        p=copy.deepcopy(self.plan)
        p['resource_changes']=[r for r in p['resource_changes'] if r['type']!='azurerm_private_dns_zone_virtual_network_link']
        for r in p['resource_changes']:
            if r.get('mode')!='managed':continue
            v=r['change']['after'];v['id']=scope.resource_ids(SUB)[r['address']]
            r['change']={'actions':['delete'],'before':v,'after':None}
        return p
    def test_reconciliation_block_create_denied_and_clear_allowed(self):
        rows=proposal(self.plan,'2026-10-24T06:00:00Z');now=dt.datetime(2026,10,24,6,30,tzinfo=dt.timezone.utc)
        with patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB):
            self.assertEqual(len(budget.check(self.plan,rows,{'resources':[]},now)),39)
            for block in (True,{'reason':'actual usage unknown'},'pending reconciliation'):
                with self.subTest(block=block),self.assertRaisesRegex(ValueError,'reconciliation'):
                    budget.check(self.plan,rows,{'resources':[],'reconciliation_block':block},now)
    def test_reconciliation_block_noop_denied_and_teardown_unblocked(self):
        rows=proposal(self.plan,'2026-10-24T06:00:00Z')['resources'];now=dt.datetime(2026,10,24,6,30,tzinfo=dt.timezone.utc)
        for row in rows:row.update(status='reserved',deleted_utc=None,plan_sha256='a'*64)
        for r in self.plan['resource_changes']:
            if r.get('mode')=='managed':r['change']['actions']=['no-op']
        ledger={'resources':rows,'reconciliation_block':{'reason':'unknown final usage'}}
        with patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB):
            with self.subTest(gate='no-op'),self.assertRaisesRegex(ValueError,'reconciliation'):budget.check(self.plan,{'resources':[]},ledger,now)
            ledger['reconciliation_block']=False
            self.assertEqual(budget.check(self.plan,{'resources':[]},ledger,now),[])
        # Teardown remains independent of admission even while its gate refuses all calls.
        with patch.object(budget,'check',side_effect=ValueError('reconciliation blocked')):teardown(self.partial_delete(),SUB)

    def test_custom_zone_unset_policy_accepts_exact_provider_unknown(self):
        for r in self.links(self.plan):r['change']['after']['resolution_policy']=None;r['change']['after_unknown']['resolution_policy']=True
        scope.validate(self.plan,SUB)
    def test_actual_partial_delete_empty_defaults_are_accepted(self):
        p=self.partial_delete()
        for r in p['resource_changes']:
            v=r['change'].get('before',{})
            if r['type']=='azurerm_linux_virtual_machine':
                for k in ('disk_encryption_set_id','secure_vm_disk_encryption_set_id','security_encryption_type'):v['os_disk'][0][k]=''
            elif r['type']=='azurerm_traffic_manager_profile':v['monitor_config'][0]['expected_status_code_ranges']=[]
            elif r['type']=='azurerm_network_interface':v['auxiliary_mode']='';v['auxiliary_sku']=''
        original=copy.deepcopy(p);teardown(p,SUB);self.assertEqual(p,original)
    def test_policy_explicit_known_unknown_and_missing_config_fail_closed(self):
        for mutation in ('Default','NxDomainRedirect','explicit_null','missing_config','nested_unknown'):
            p=copy.deepcopy(self.plan);r=self.links(p)[0]
            if mutation in ('Default','NxDomainRedirect'):r['change']['after']['resolution_policy']=mutation
            elif mutation=='explicit_null':self.config(p,r['type'])['resolution_policy']={'constant_value':None}
            elif mutation=='missing_config':
                cs=p['configuration']['root_module']['module_calls']['dns']['module']['resources'];cs[:]=[c for c in cs if c['address']!=r['type']+'.this']
            else:r['change']['after']['resolution_policy']=None;r['change']['after_unknown']['resolution_policy']={'foreign':True}
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):scope.validate(p,SUB)
    def test_only_exact_empty_paths_normalize_and_explicit_inputs_fail_closed(self):
        paths=[('azurerm_linux_virtual_machine','os_disk',k,'') for k in ('disk_encryption_set_id','secure_vm_disk_encryption_set_id','security_encryption_type')]
        paths += [('azurerm_traffic_manager_profile','monitor_config','expected_status_code_ranges',[])]
        paths += [('azurerm_network_interface',None,k,'') for k in ('auxiliary_mode','auxiliary_sku')]
        for typ,block,key,empty in paths:
            for mutation in ('nonempty','explicit','unknown','wrong_empty'):
                p=copy.deepcopy(self.plan);r=next(r for r in p['resource_changes'] if r['type']==typ);v=r['change']['after'];target=v[block][0] if block else v;target[key]=empty
                if mutation=='nonempty':target[key]=['200-299'] if isinstance(empty,list) else 'foreign'
                elif mutation=='explicit':
                    expressions=self.config(p,typ)
                    if block:expressions[block]=[{key:{'constant_value':empty}}]
                    else:expressions[key]={'constant_value':empty}
                elif mutation=='wrong_empty':target[key]={}
                else:
                    target[key]=None
                    if block:r['change']['after_unknown'][block]=[{key:True}]
                    else:r['change']['after_unknown'][key]=True
                with self.subTest(typ=typ,key=key,mutation=mutation),self.assertRaises(ValueError):scope.validate(p,SUB)
    def test_absent_policy_known_empty_noop_delete(self):
        for r in self.links(self.plan):r['change']['after']['resolution_policy']=''
        scope.validate(self.plan,SUB)
        for r in self.plan['resource_changes']:
            if r.get('mode')!='managed':continue
            v=r['change']['after'];v['id']=scope.resource_ids(SUB)[r['address']]
            r['change']={'actions':['no-op'],'before':copy.deepcopy(v),'after':v,'after_unknown':{}}
        scope.validate(self.plan,SUB)
        for r in self.plan['resource_changes']:
            if r.get('mode')=='managed':r['change']['actions']=['delete'];r['change']['after']=None
        teardown(self.plan,SUB)

if __name__=='__main__':unittest.main()
