"""Task10B native changed-resource fixtures plus explicit plan input/data/expression context."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import tempfile
import datetime as dt
import os
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import azure_budget as budget
SUB='11111111-1111-1111-1111-111111111111'
ROOT=Path(__file__).resolve().parents[2]


def fixture():
    plan=json.loads((Path(__file__).parent/'fixtures/probe-native-plan.json').read_text())
    plan['variables']={k:{'value':v} for k,v in dict(subscription_id=SUB,probe_site='azure-a',location='westus',ttl='2026-10-25T00:00:00Z',parent_ttl='2026-12-01T00:00:00Z',campaign_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',vm_size='Standard_B2pts_v2',image_version='24.04.202610210',owner_peer_cidrs=['8.8.8.8/32']).items()}
    plan['configuration']={'root_module':{'resources':[{'address':'azurerm_network_interface.probe','expressions':{'ip_configuration':[{}]}}],'module_calls':{'network':{'module':{'resources':[
        {'address':'azurerm_subnet_network_security_group_association.this','expressions':{'subnet_id':{'references':['azurerm_subnet.this','each.key']},'network_security_group_id':{'references':['azurerm_network_security_group.this.id','azurerm_network_security_group.this']}}},
        {'address':'azurerm_subnet_route_table_association.this','expressions':{'subnet_id':{'references':['azurerm_subnet.this','each.key']},'route_table_id':{'references':['azurerm_route_table.this.id','azurerm_route_table.this']}}}
    ]}}}}}
    plan['configuration']['root_module']['module_calls']['probe']={'module':{'resources':[{'address':'azapi_update_resource.disk_tags','expressions':{}}]}}
    plan['planned_values']={'root_module':{'child_modules':[{'resources':[{'address':'module.network.data.azurerm_resource_group.project','mode':'data','type':'azurerm_resource_group','values':{'id':f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab','name':'rg-pathwatch-lab','tags':dict(project='pathwatch',owner='codex-pathwatch',ttl='2026-12-01T00:00:00Z',est_usd_per_hour='0.001')}}]}]}}
    return plan


class ProbeScopeTests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/azure_probe_scope.py'
        self.assertTrue(path.is_file(),'missing exact cloud probe admission scope')
        spec=importlib.util.spec_from_file_location('probe_scope',path);self.scope=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.scope)
        self.plan=fixture()
    def test_native_new_network_nic_pip_vm_and_disk_are_complete(self):
        self.assertEqual(self.scope.validate(self.plan,SUB),'azure-a')
    def test_missing_component_foreign_resource_and_update_are_denied(self):
        for index in range(len(self.plan['resource_changes'])):
            plan=copy.deepcopy(self.plan);plan['resource_changes'].pop(index)
            with self.subTest(index=index),self.assertRaises(ValueError):self.scope.validate(plan,SUB)
        plan=copy.deepcopy(self.plan);plan['resource_changes'][0]['change']['actions']=['update']
        with self.assertRaises(ValueError):self.scope.validate(plan,SUB)
    def test_vm_nic_ip_patch_and_region_mutations_are_denied(self):
        for kind,field,value in [('azurerm_linux_virtual_machine','size','Standard_NC6'),('azurerm_linux_virtual_machine','custom_data','evil'),('azurerm_linux_virtual_machine','priority','Spot'),('azurerm_network_interface','ip_forwarding_enabled',False),('azurerm_public_ip','sku','Basic'),('azapi_update_resource','body',{'properties':{}})]:
            with self.subTest(kind=kind,field=field):
                plan=copy.deepcopy(self.plan);r=next(r for r in plan['resource_changes'] if r['type']==kind);r['change']['after'][field]=value
                with self.assertRaises(ValueError):self.scope.validate(plan,SUB)
    def test_current_invalid_parent_cannot_fall_back_to_valid_planned_parent(self):
        plan=copy.deepcopy(self.plan)
        plan['resource_changes'].append(dict(address='module.network.data.azurerm_resource_group.project',mode='data',type='azurerm_resource_group',change=dict(actions=['read'],after={},after_unknown={'id':True})))
        with self.assertRaises(ValueError):self.scope.validate(plan,SUB)
    def test_unknown_cost_addons_and_malformed_parent_ttl_are_denied(self):
        for typ,field in [('azurerm_public_ip','public_ip_prefix_id'),('azurerm_network_interface','dns_servers')]:
            plan=copy.deepcopy(self.plan)
            next(r for r in plan['resource_changes'] if r['type']==typ)['change']['after_unknown'][field]=True
            with self.subTest(typ=typ),self.assertRaises(ValueError):self.scope.validate(plan,SUB)
        plan=copy.deepcopy(self.plan);plan['variables']['parent_ttl']['value']='zzz'
        plan['planned_values']['root_module']['child_modules'][0]['resources'][0]['values']['tags']['ttl']='zzz'
        with self.assertRaises(ValueError):self.scope.validate(plan,SUB)

    def test_generic_nva_plan_does_not_become_probe_scope(self):
        self.assertFalse(self.scope.matches({'resource_changes':[{'address':'module.appliance.azurerm_linux_virtual_machine.this["nva"]','mode':'managed','type':'azurerm_linux_virtual_machine'}]}))


class ProbeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.cache');self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name);self.plan=fixture();self.now=budget.utc('2026-10-24T20:00:00Z')
        self.addCleanup(patch.stopall);patch.object(budget,'ROOT',self.directory).start();patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB).start()
        self.ledger={'resources':[]}
        for address,rid in [('azurerm_resource_group.project',f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab'),('azurerm_storage_account.state',f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Storage/storageAccounts/pwstate')]:
            self.ledger['resources'].append(dict(address=address,type=address.split('.')[0],subscription_id=SUB,resource_id=rid,start_utc='2026-10-24T00:00:00Z',end_utc='2026-12-01T00:00:00Z',status='applied',deleted_utc=None,plan_sha256='a'*64,estimate_usd='0.2'))
        self.manifest={}
        records=[('vm_hours','Virtual Machines Bpsv2 Series','B2pts v2','B2pts v2','1 Hour',0.009),('ip_hours','IP Addresses','Standard','Standard IPv4 Static Public IP','1 Hour',0.005),('disk_month','Standard HDD Managed Disks','S4 LRS','S4 LRS Disk','1/Month',1.54),('transactions','Standard HDD Managed Disks','S4 LRS','S4 LRS Disk Operations','10K',0.0005),('egress_gb','Rtn Preference: MGN','Standard','Standard Data Transfer Out','1 GB',0.087)]
        for category,product,sku,meter,unit,price in records:
            selector=dict(meterId=category,skuId='sku-'+category,armRegionName='westus',unitOfMeasure=unit,productName=product,skuName=sku,meterName=meter,tierMinimumUnits=100 if category=='egress_gb' else 0)
            capture=category+'.json';self.manifest[category]=dict(capture=capture,selector=selector)
            (self.directory/capture).write_text(json.dumps(dict(captured_at='2026-10-24T19:59:00Z',url='https://prices.azure.com/api/retail/prices?fixture',response=dict(Items=[dict(selector,currencyCode='USD',type='Consumption',effectiveStartDate='2026-01-01T00:00:00Z',retailPrice=price)]))))
        self.preflight=dict(subscription_id=SUB,location='westus',captured_at='2026-10-24T19:59:00Z',usage=[dict(name={'value':'cores'},currentValue=0,limit=6),dict(name={'value':'standardBPSv2Family'},currentValue=0,limit=6)],skus=[dict(name='Standard_B2pts_v2',resourceType='virtualMachines',family='standardBPSv2Family',locations=['westus'],restrictions=[],capabilities=[dict(name='vCPUs',value='2'),dict(name='CpuArchitectureType',value='Arm64'),dict(name='HyperVGenerations',value='V2')])],image=dict(architecture='Arm64',hyperVGeneration='V2',name='24.04.202610210',osDiskImage={'operatingSystem':'Linux'}))
        (self.directory/'preflight.json').write_text(json.dumps(self.preflight))
        self.preflight['image_urn']='Canonical:ubuntu-24_04-lts:server-arm64:24.04.202610210'
        self.preflight['image'].update(id=f'/subscriptions/{SUB}/providers/Microsoft.Compute/locations/westus/publishers/Canonical/artifactTypes/VMImage/offers/ubuntu-24_04-lts/skus/server-arm64/versions/24.04.202610210',location='westus')
        (self.directory/'preflight.json').write_text(json.dumps(self.preflight))
        self.manifest['network_capture']='network.json'
        (self.directory/'network.json').write_text(json.dumps(dict(captured_at='2026-10-24T19:59:00Z',url='https://prices.azure.com/api/retail/prices?fixture',response={'Items':[]})))
        self.path=ROOT/'scripts/probe_proposal.py'
    def proposal(self):
        self.assertTrue(self.path.is_file(),'missing complete cloud probe proposal and billing path')
        spec=importlib.util.spec_from_file_location('probe_proposal',self.path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.build(self.plan,self.manifest,'2026-10-24T20:00:00Z','preflight.json',False,self.ledger,self.now)
    def test_unknown_forbidden_configuration_refused_by_check_and_proposal(self):
        original=copy.deepcopy(self.plan);proposal=self.proposal()
        cases=[('azapi_update_resource',(field,),True) for field in
               ('sensitive_body','sensitive_body_version','update_headers','update_query_parameters','read_headers','read_query_parameters')]
        cases += [('azurerm_linux_virtual_machine',('os_disk',0,field),True) for field in
                  ('diff_disk_settings','disk_encryption_set_id','secure_vm_disk_encryption_set_id','security_encryption_type','write_accelerator_enabled')]
        cases += [('azurerm_linux_virtual_machine',('os_disk',),True),
                  ('azurerm_linux_virtual_machine',('os_disk',),[True]),
                  ('azurerm_virtual_network',('ddos_protection_plan',),True),
                  ('azurerm_virtual_network',('ddos_protection_plan',),[{'id':True}]),
                  ('azurerm_public_ip',('ddos_protection_mode',),True),
                  ('azurerm_linux_virtual_machine',('priority',),True),
                  ('azurerm_network_security_rule',('destination_address_prefixes',),True),
                  ('azurerm_network_security_rule',('destination_port_ranges',),True)]
        for typ,path,value in cases:
            for entrypoint in ('canonical','proposal'):
                self.plan=copy.deepcopy(original)
                unknown=next(r for r in self.plan['resource_changes'] if r['type']==typ)['change']['after_unknown']
                for part in path[:-1]:unknown=unknown[part]
                unknown[path[-1]]=value
                with self.subTest(typ=typ,path=path,value=value,entrypoint=entrypoint),self.assertRaises(ValueError):
                    if entrypoint=='canonical':budget.check(self.plan,copy.deepcopy(proposal),self.ledger,self.now)
                    else:self.proposal()

    def test_write_only_configuration_requires_exact_context_and_no_expression(self):
        original=copy.deepcopy(self.plan);proposal=self.proposal()
        mutations=('sensitive-ref','sensitive-null','version-ref','version-null','missing-module','missing-resource','foreign-address','duplicate-resource','missing-expressions')
        for mutation in mutations:
            for entrypoint in ('canonical','proposal'):
                self.plan=copy.deepcopy(original)
                calls=self.plan['configuration']['root_module']['module_calls'];resources=calls['probe']['module']['resources'];resource=resources[0]
                if mutation.startswith(('sensitive-','version-')):
                    field='sensitive_body' if mutation.startswith('sensitive-') else 'sensitive_body_version'
                    resource['expressions'][field]={'constant_value':None} if mutation.endswith('-null') else {'references':['var.unreviewed_body']}
                elif mutation=='missing-module':del calls['probe']
                elif mutation=='missing-resource':resources.clear()
                elif mutation=='foreign-address':resource['address']='azapi_update_resource.foreign'
                elif mutation=='duplicate-resource':resources.append(copy.deepcopy(resource))
                elif mutation=='missing-expressions':del resource['expressions']
                with self.subTest(mutation=mutation,entrypoint=entrypoint),self.assertRaises(ValueError):
                    if entrypoint=='canonical':budget.check(self.plan,copy.deepcopy(proposal),self.ledger,self.now)
                    else:self.proposal()

    def test_native_computed_id_and_defaults_are_not_forbidden_configuration(self):
        by={r['type']:r['change']['after_unknown'] for r in self.plan['resource_changes']}
        self.assertTrue(by['azurerm_linux_virtual_machine']['os_disk'][0]['id'])
        self.assertTrue(by['azapi_update_resource']['output'])
        self.assertTrue(by['azurerm_network_interface']['ip_configuration'][0]['gateway_load_balancer_frontend_ip_configuration_id'])
        self.assertTrue(by['azurerm_virtual_network']['subnet'])
        self.assertEqual(len(self.proposal()['resources']),21)
        # Explicit empty/known forbidden fields remain valid, unlike unknown ones.
        resource=next(r for r in self.plan['resource_changes'] if r['type']=='azapi_update_resource')
        resource['change']['after_unknown'].update(sensitive_body=False,update_headers={})
        self.assertEqual(len(self.proposal()['resources']),21)

    def test_locked_reserve_check_preserves_history_and_freeze(self):
        proposal=self.proposal();ledger_path=self.directory/'ledger.json';ledger_path.write_text(json.dumps(self.ledger))
        patch.object(budget,'LEDGER',ledger_path).start()
        now=self.now
        class FixedDatetime(dt.datetime):
            @classmethod
            def now(cls,tz=None):return now
        patch.object(budget.dt,'datetime',FixedDatetime).start()
        digest='d'*64;original=copy.deepcopy(self.ledger['resources'])
        budget.admit('reserve',self.plan,copy.deepcopy(proposal),digest);reserved=ledger_path.read_bytes()
        budget.admit('check',self.plan,copy.deepcopy(proposal),digest)
        self.assertEqual(ledger_path.read_bytes(),reserved);self.assertEqual(json.loads(reserved)['resources'][:2],original)
        frozen=json.loads(reserved);frozen['reconciliation_block']=True;ledger_path.write_text(json.dumps(frozen))
        with self.assertRaisesRegex(ValueError,'reconciliation'):budget.admit('check',self.plan,copy.deepcopy(proposal),digest)

    def test_full_probe_proposal_admits_on_unblocked_fixture(self):
        result=self.proposal();self.assertEqual(len(result['resources']),21)
        vm=next(r for r in result['resources'] if r['type']=='azurerm_linux_virtual_machine')
        self.assertEqual({k:budget.money(v) for k,v in vm['bounds'].items()},{'vm_hours':budget.money(4),'egress_gb':budget.money('0.020')})
        self.assertEqual(vm['campaign_id'],'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
    def test_frozen_ledger_denies_before_probe_dispatch(self):
        self.ledger['reconciliation_block']=True
        with self.assertRaisesRegex(ValueError,'reconciliation'):self.proposal()
    def test_quota_sku_architecture_and_freshness_are_real_evidence(self):
        for mutation in ('quota','restriction','architecture','stale'):
            with self.subTest(mutation=mutation):
                value=copy.deepcopy(self.preflight)
                if mutation=='quota':value['usage'][0]['currentValue']=5
                if mutation=='restriction':value['skus'][0]['restrictions']=[{'type':'Location'}]
                if mutation=='architecture':value['image']['architecture']='x64'
                if mutation=='stale':value['captured_at']='2026-10-23T19:00:00Z'
                (self.directory/'preflight.json').write_text(json.dumps(value))
                with self.assertRaises(ValueError):self.proposal()
    def test_active_known_address_cannot_hide_foreign_identity(self):
        self.ledger['resources'].append(dict(address='module.probe.azurerm_linux_virtual_machine.probe["azure-b"]',type='azurerm_linux_virtual_machine',region='swedencentral',status='applied',campaign_id='legacy',estimate_usd='0.5'))
        with self.assertRaises(ValueError):self.proposal()
    def test_missing_network_capture_items_is_not_free_evidence(self):
        path=self.directory/'network.json';value=json.loads(path.read_text());del value['response']['Items'];path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.proposal()

    def test_active_foreign_vm_blocks_second_permanent_vm(self):
        self.ledger['resources'].append(dict(address='module.appliance.azurerm_linux_virtual_machine.this["nva"]',type='azurerm_linux_virtual_machine',region='westus',status='applied',estimate_usd='0.5'))
        with self.assertRaises(ValueError):self.proposal()

    def as_site_b(self):
        self.plan=json.loads(json.dumps(self.plan).replace('azure-a','azure-b').replace('10.10','10.20').replace('westus','swedencentral').replace('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'))
        self.preflight['location']='swedencentral';self.preflight['skus'][0]['locations']=['swedencentral']
        self.preflight['image']['location']='swedencentral';self.preflight['image']['id']=self.preflight['image']['id'].replace('/westus/','/swedencentral/')
        (self.directory/'preflight.json').write_text(json.dumps(self.preflight))
        for category,item in self.manifest.items():
            if category=='network_capture':continue
            item['selector']['armRegionName']='swedencentral'
            path=self.directory/item['capture'];value=json.loads(path.read_text());value['response']['Items'][0]['armRegionName']='swedencentral';path.write_text(json.dumps(value))
    def test_second_b_campaign_coexists_with_one_active_a(self):
        result=self.proposal()
        for row in result['resources']:row.update(status='applied',plan_sha256='b'*64,deleted_utc=None)
        self.ledger['resources'].extend(result['resources'])
        self.ledger['resources'].append(dict(type='azurerm_linux_virtual_machine',region='swedencentral',campaign_id='old-dns-campaign',status='deleted',estimate_usd='0.5'))
        self.as_site_b()
        self.assertEqual(len(self.proposal()['resources']),21)
    def test_third_b_campaign_and_ended_id_reuse_rejected(self):
        self.as_site_b()
        for campaign in ('old-dns-campaign','second-used-campaign'):
            self.ledger['resources'].append(dict(type='azurerm_linux_virtual_machine',region='swedencentral',campaign_id=campaign,status='deleted',estimate_usd='0.5'))
        with self.assertRaisesRegex(ValueError,'two B'):self.proposal()
        self.plan['variables']['campaign_id']['value']='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        self.ledger['resources'][-1]['campaign_id']='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        with self.assertRaisesRegex(ValueError,'ended|foreign'):self.proposal()
    def test_b_timebox_no_reset_and_cumulative_cap(self):
        result=self.proposal()
        for row in result['resources']:row.update(status='reserved',plan_sha256='b'*64,deleted_utc=None)
        self.ledger['resources'].extend(result['resources'])
        self.plan['variables']['ttl']['value']='2026-10-25T01:00:00Z'
        for resource in self.plan['resource_changes']:
            v=resource['change']['after'];tags=v.get('body',{}).get('tags',{}) if resource['type']=='azapi_update_resource' else v.get('tags',{})
            if tags:tags['ttl']='2026-10-25T01:00:00Z'
        with self.assertRaisesRegex(ValueError,'original timebox'):self.proposal()
        self.plan=fixture();self.ledger['resources'].append(dict(estimate_usd='18',status='deleted'))
        with self.assertRaisesRegex(ValueError,'18 USD'):self.proposal()
    def test_all_usage_categories_and_exact_meter_sku_are_mandatory(self):
        result=self.proposal();row=next(r for r in result['resources'] if r['type']=='azurerm_linux_virtual_machine')
        row['bounds'].pop('egress_gb')
        with self.assertRaises(ValueError):budget.check(self.plan,result,self.ledger,self.now)
        result=self.proposal();row=next(r for r in result['resources'] if r['type']=='azurerm_linux_virtual_machine')
        row['meters'][0]['selector']['productName']='Virtual Machines Bpsv2 Series Windows'
        with self.assertRaisesRegex(ValueError,'nonSpot Linux'):budget.check(self.plan,result,self.ledger,self.now)

    def materialize(self):
        result=self.proposal()
        prefix=f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/'
        by={r['address']:r for r in result['resources']}
        for resource in self.plan['resource_changes']:
            change=resource['change'];value=change['after'];value['id']=by[resource['address']]['resource_id']
            if resource['type'].endswith('_association'):
                key=resource['address'].split('["')[-1].split('"')[0]
                value['subnet_id']=prefix+'virtualNetworks/vnet-pathwatch-azure-a/subnets/snet-'+key
                if 'security_group' in resource['type']:value['network_security_group_id']=prefix+'networkSecurityGroups/nsg-pathwatch-azure-a'
                else:value['route_table_id']=prefix+'routeTables/rt-pathwatch-azure-a'
            change['before']=copy.deepcopy(value);change['actions']=['no-op'];change['after_unknown']={}
        for row in result['resources']:row.update(status='applied',plan_sha256='b'*64,deleted_utc=None)
        self.ledger['resources'].extend(result['resources'])
        return dict(purpose='cloud-probe',resources=[])
    def test_complete_noop_requires_actual_current_ids_and_metadata(self):
        result=self.materialize()
        self.assertEqual(budget.check(self.plan,result,self.ledger,self.now),[])
        resource=next(r for r in self.plan['resource_changes'] if r['type']=='azurerm_public_ip')
        del resource['change']['after']['id'];del resource['change']['before']['id']
        with self.assertRaises(ValueError):budget.check(self.plan,result,self.ledger,self.now)
    def test_delete_only_remains_available_with_frozen_ledger(self):
        self.materialize()
        import azure_teardown
        for resource in self.plan['resource_changes']:
            resource['change']['actions']=['delete'];resource['change']['after']=None
        azure_teardown.validate(self.plan,SUB)
        self.plan['resource_changes'][0]['change']['before']['id']='foreign'
        with self.assertRaises(ValueError):azure_teardown.validate(self.plan,SUB)

if __name__=='__main__':unittest.main()
