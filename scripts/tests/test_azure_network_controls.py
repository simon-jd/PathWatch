"""Network admission/teardown regressions: synthetic plans, no cloud calls."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
budget=load('azure_budget');teardown=load('azure_teardown')
SUB='11111111-1111-1111-1111-111111111111'
PREFIX=f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/'
SOURCE='docs/azure-prices/2026-10-23-network-components.json'

class NetworkControlsTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.dict(os.environ,{'AZURE_SUBSCRIPTION_ID':SUB});self.patch.start();self.addCleanup(self.patch.stop)
        self.now=budget.utc(json.loads((ROOT/'docs/azure-prices/2026-10-23-network-westus.json').read_text())['captured_at'])+dt.timedelta(seconds=1)
        self.end=(self.now+dt.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        tags={'project':'pathwatch','owner':'codex-pathwatch','ttl':self.end,'est_usd_per_hour':'0'}
        parent={'resource_group_name':'rg-pathwatch-lab','location':'westus','tags':tags}
        name='pathwatch-azure-a'
        resources={
            'azurerm_virtual_network.this': parent|{'name':'vnet-'+name,'id':PREFIX+'virtualNetworks/vnet-'+name,'address_space':['10.10.0.0/16']},
            'azurerm_network_security_group.this': parent|{'name':'nsg-'+name,'id':PREFIX+'networkSecurityGroups/nsg-'+name},
            'azurerm_route_table.this': parent|{'name':'rt-'+name,'id':PREFIX+'routeTables/rt-'+name},
            'azurerm_network_security_rule.deny_ingress': {'name':'deny-all-inbound','resource_group_name':'rg-pathwatch-lab','network_security_group_name':'nsg-'+name,'direction':'Inbound','access':'Deny','priority':4096,'protocol':'*','source_address_prefix':'*','destination_address_prefix':'*','source_port_range':'*','destination_port_range':'*','id':PREFIX+'networkSecurityGroups/nsg-'+name+'/securityRules/deny-all-inbound'},
        }
        for key,cidr in {'workloads':'10.10.0.0/24','nva':'10.10.1.0/27','aks_nodes':'10.10.16.0/20'}.items():
            subnet=PREFIX+'virtualNetworks/vnet-'+name+'/subnets/snet-'+key
            resources[f'azurerm_subnet.this["{key}"]']={'id':subnet,'name':'snet-'+key,'resource_group_name':'rg-pathwatch-lab','virtual_network_name':'vnet-'+name,'address_prefixes':[cidr],'default_outbound_access_enabled':False}
            resources[f'azurerm_subnet_network_security_group_association.this["{key}"]']={'id':subnet,'subnet_id':subnet,'network_security_group_id':PREFIX+'networkSecurityGroups/nsg-'+name}
            if key!='nva': resources[f'azurerm_subnet_route_table_association.this["{key}"]']={'id':subnet,'subnet_id':subnet,'route_table_id':PREFIX+'routeTables/rt-'+name}
        self.plan={'variables':{'subscription_id':{'value':SUB},'location':{'value':'westus'}},'resource_changes':[
            {'mode':'managed','address':'module.network.'+address,'type':address.split('.')[0],'change':{'actions':['create'],'after':values,'after_unknown':{}}} for address,values in resources.items()]}
        self.proposal={'resources':[{'address':r['address'],'type':r['type'],'region':'westus','start_utc':(self.now-dt.timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),'end_utc':self.end,'estimate_usd':0,'meters':[],'price_source':SOURCE} for r in self.plan['resource_changes']]}

    def check(self):return budget.check(self.plan,self.proposal,{'resources':[{'estimate_usd':'0.30'}]},self.now)
    def resource(self,typ):return next(r for r in self.plan['resource_changes'] if r['type']==typ)
    def test_zero_price_network_still_requires_complete_admission(self):self.check()
    def test_zero_price_still_obeys_historical_cap(self):
        with self.assertRaises(ValueError):budget.check(self.plan,self.proposal,{'resources':[{'estimate_usd':18.01}]},self.now)
    def test_price_source_required(self):
        del self.proposal['resources'][0]['price_source']
        with self.assertRaises(ValueError):self.check()
    def test_stale_real_capture_refused_with_otherwise_active_timebox(self):
        self.now+=dt.timedelta(hours=25)
        end=(self.now+dt.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        for row in self.proposal['resources']:row['end_utc']=end
        for r in self.plan['resource_changes']:
            tags=r['change']['after'].get('tags')
            if tags:tags['ttl']=end
        with self.assertRaisesRegex(ValueError,'stale'):self.check()
    def test_zero_resource_missing_entry_refused(self):
        self.proposal['resources'].pop()
        with self.assertRaises(ValueError):self.check()
    def test_billed_gateway_cannot_enter_zero_price_allowlist(self):
        r=self.resource('azurerm_route_table');r['type']='azurerm_nat_gateway'
        with self.assertRaises(ValueError):self.check()
    def test_foreign_group_id_and_parent_ownership_refused(self):
        for key,value in [('id',PREFIX.replace('rg-pathwatch-lab','rg-mlinfra-westus')+'virtualNetworks/foreign'),('tags',{'project':'warden','owner':'codex-warden'})]:
            with self.subTest(key=key):
                saved=copy.deepcopy(self.plan);self.resource('azurerm_virtual_network')['change']['after'][key]=value
                with self.assertRaises(ValueError):self.check()
                self.plan=saved
    def test_foreign_subnet_even_in_same_group_refused(self):
        self.resource('azurerm_subnet_route_table_association')['change']['after']['subnet_id']=PREFIX+'virtualNetworks/another/subnets/foreign'
        with self.assertRaises(ValueError):self.check()
    def test_unknown_association_needs_actual_own_resource_reference(self):
        r=self.resource('azurerm_subnet_route_table_association');r['change']['after']['subnet_id']=None;r['change']['after_unknown']['subnet_id']=True
        with self.assertRaises(ValueError):self.check()
    def test_actual_unknown_reference_shape_accepted_and_foreign_reference_refused(self):
        r=self.resource('azurerm_subnet_route_table_association');r['change']['after']['subnet_id']=None;r['change']['after_unknown']['subnet_id']=True
        expressions={'subnet_id':{'references':['azurerm_subnet.this','each.key']}}
        config={'resources':[{'address':'azurerm_subnet_route_table_association.this','expressions':expressions}]}
        self.plan['configuration']={'root_module':{'module_calls':{'network':{'module':config}}}}
        self.check()
        expressions['subnet_id']['references'].append('data.azurerm_subnet.foreign.id')
        with self.assertRaises(ValueError):self.check()
    def test_missing_default_deny_and_inline_bypass_refused(self):
        r=self.resource('azurerm_network_security_rule');self.plan['resource_changes'].remove(r)
        self.proposal['resources']=[r for r in self.proposal['resources'] if r['type']!='azurerm_network_security_rule']
        with self.assertRaises(ValueError):self.check()
    def test_inline_paid_or_open_children_refused(self):
        for typ,field in [('azurerm_network_security_group','security_rule'),('azurerm_route_table','route'),('azurerm_virtual_network','subnet')]:
            saved=copy.deepcopy(self.plan);self.resource(typ)['change']['after'][field]=[{'unsafe':True}]
            with self.assertRaises(ValueError):self.check()
            self.plan=saved
    def test_default_route_or_open_security_rule_refused(self):
        self.resource('azurerm_network_security_rule')['change']['after']['access']='Allow'
        with self.assertRaises(ValueError):self.check()
    def destroy(self):
        plan=copy.deepcopy(self.plan)
        for r in plan['resource_changes']:r['change']={'actions':['delete'],'before':r['change']['after'],'after':None}
        return plan
    def test_network_destroy_retains_shared_rg_and_state(self):teardown.validate(self.destroy(),SUB)
    def test_invalid_actual_vnet_address_spaces_rejected_for_admission_and_delete(self):
        original=copy.deepcopy(self.plan)
        invalid=[[], ['10.10.0.0/16','10.30.0.0/16'], ['192.168.0.0/16','172.16.0.0/16'], ['192.168.0.0/16'], ['10.20.0.0/16'], None, '10.10.0.0/16']
        for spaces in invalid:
            for deleting in (False,True):
                self.plan=copy.deepcopy(original)
                self.resource('azurerm_virtual_network')['change']['after']['address_space']=spaces
                with self.subTest(spaces=spaces,deleting=deleting),self.assertRaisesRegex(ValueError,'address_space|assigned Azure /16|Regional network CIDR'):
                    if deleting:teardown.validate(self.destroy(),SUB)
                    else:self.check()
        self.plan=original
    def test_fixture_actual_vnet_must_match_declared_assigned_cidr(self):
        # A coherent assigned /16 topology still must match the prepared fixture's
        # declared range, even though fixture names do not use regional a/b maps.
        self.plan=json.loads(json.dumps(self.plan).replace('pathwatch-azure-a','pathwatch-it-abcdef123456').replace('10.10.','10.20.'))
        self.plan['variables'].update(name={'value':'pathwatch-it-abcdef123456'},address_space={'value':'10.10.0.0/16'})
        for deleting in (False,True):
            with self.subTest(deleting=deleting),self.assertRaisesRegex(ValueError,'CIDR|address_space'):
                if deleting:teardown.validate(self.destroy(),SUB)
                else:self.check()
    def test_no_vnet_fallback_only_supports_partial_teardown(self):
        plan=self.destroy()
        # A partial apply can create NSG/route table while VNet creation fails;
        # cleanup must retain a safe route using only the owned parent before-state.
        plan['resource_changes']=[r for r in plan['resource_changes'] if r['type'] in {'azurerm_network_security_group','azurerm_route_table'}]
        teardown.validate(plan,SUB)
        fixture=json.loads(json.dumps(plan).replace('pathwatch-azure-a','pathwatch-it-abcdef123456'))
        fixture['variables'].update(name={'value':'pathwatch-it-abcdef123456'},address_space={'value':'10.10.0.0/16'})
        teardown.validate(fixture,SUB)
    def test_admission_still_requires_actual_vnet_resource(self):
        self.plan['resource_changes']=[r for r in self.plan['resource_changes'] if r['type']!='azurerm_virtual_network']
        with self.assertRaisesRegex(ValueError,'requires the owned VNet'):self.check()
    def test_destroy_foreign_parent_or_shared_rg_refused(self):
        for typ in ['azurerm_resource_group','azurerm_storage_account']:
            plan=self.destroy();plan['resource_changes'][0]['type']=typ
            with self.assertRaises(ValueError):teardown.validate(plan,SUB)
        plan=self.destroy();plan['resource_changes'][0]['change']['before']['id']=PREFIX+'virtualNetworks/foreign'
        with self.assertRaises(ValueError):teardown.validate(plan,SUB)
    def test_network_destroy_never_create_update_or_replace(self):
        for actions in [['create'],['update'],['delete','create']]:
            plan=self.destroy();plan['resource_changes'][0]['change']['actions']=actions
            with self.assertRaises(ValueError):teardown.validate(plan,SUB)

if __name__=='__main__':unittest.main()
