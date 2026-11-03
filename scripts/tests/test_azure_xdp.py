"""Synthetic local plans only; never cloud permissions or a real apply plan."""
import datetime as dt
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import azure_budget as budget
SUB='11111111-1111-1111-1111-111111111111'
class DispatchTests(unittest.TestCase):
    def test_unknown_explicit_scope_never_falls_through_to_legacy(self):
        plan={'variables':{'subscription_id':{'value':SUB}},'resource_changes':[]}
        with patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB):
            with self.assertRaisesRegex(ValueError,'scope'):
                budget.check(plan,{'scope':'xdp-typo','resources':[]},{'resources':[]},dt.datetime.now(dt.timezone.utc))

import copy
import hashlib
import json
import subprocess
import tempfile
from types import SimpleNamespace
import azure_xdp_scope as scope
import azure_xdp_lifecycle as life
import azure_teardown as teardown
ROOT=Path(__file__).resolve().parents[2]
KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch'
START='2026-10-25T00:00:00Z';END='2026-10-25T02:00:00Z';NOW=budget.utc('2026-10-25T00:30:00Z')

def context(kind='xdp-runner'):
    return dict(source_hashes=dict(life.BOOTSTRAP_HASHES),scope=kind,subscription_id=SUB,location='westus',run_id='test0001',start_utc=START,ttl=END,operator_ipv4_cidr='8.8.8.8/32',operator_principal_id='22222222-2222-2222-2222-222222222222',admin_public_key=KEY,hourly={'vm':'0.17','ip':'0.005','disk':'0.505','backend':'0.085'})

def fixture(kind='xdp-runner'):
    """Independently authored plan-shaped values, not scope.expected output."""
    prefix=f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab';arm=prefix+'/providers/';suffix='pathwatch-xdp-test0001'
    tags=lambda rate:dict(project='pathwatch',owner='codex-pathwatch',ttl=END,est_usd_per_hour=rate)
    parent=lambda short,rate='0':dict(name=short+suffix,resource_group_name='rg-pathwatch-lab',location='westus',tags=tags(rate))
    vnet=arm+'Microsoft.Network/virtualNetworks/vnet-'+suffix;nic=arm+'Microsoft.Network/networkInterfaces/nic-'+suffix;nsg=arm+'Microsoft.Network/networkSecurityGroups/nsg-'+suffix;pip=arm+'Microsoft.Network/publicIPAddresses/pip-'+suffix
    if kind=='xdp-runner':
        values={
            'azurerm_virtual_network.runner':parent('vnet-')|{'address_space':['10.203.0.0/24'],'dns_servers':[]},
            'azurerm_subnet.runner':dict(name='runner',resource_group_name='rg-pathwatch-lab',virtual_network_name='vnet-'+suffix,address_prefixes=['10.203.0.0/27']),
            'azurerm_network_security_group.runner':parent('nsg-')|{'security_rule':[dict(name='operator-ssh',priority=100,direction='Inbound',access='Allow',protocol='Tcp',source_port_range='*',destination_port_range='22',source_address_prefix='8.8.8.8/32',destination_address_prefix='10.203.0.4')]},
            'azurerm_public_ip.runner':parent('pip-','0.005')|dict(allocation_method='Static',sku='Standard',sku_tier='Regional',ip_version='IPv4'),
            'azurerm_network_interface.runner':parent('nic-')|dict(ip_forwarding_enabled=False,accelerated_networking_enabled=False,dns_servers=[],ip_configuration=[dict(name='primary',primary=True,private_ip_address_allocation='Static',private_ip_address='10.203.0.4',subnet_id=vnet+'/subnets/runner',public_ip_address_id=pip)]),
            'azurerm_network_interface_security_group_association.runner':dict(network_interface_id=nic,network_security_group_id=nsg),
            'azurerm_linux_virtual_machine.runner':parent('vm-','0.17')|dict(size='Standard_B2ps_v2',admin_username='pathwatch',disable_password_authentication=True,network_interface_ids=[nic],provision_vm_agent=True,allow_extension_operations=False,patch_mode='ImageDefault',priority='Regular',admin_ssh_key=[dict(username='pathwatch',public_key=KEY)],os_disk=[dict(name='os-'+suffix,caching='ReadWrite',storage_account_type='Standard_LRS',disk_size_gb=32)],source_image_reference=[dict(publisher='Canonical',offer='ubuntu-24_04-lts',sku='server-arm64',version='24.04.202610210')]),
            'azapi_update_resource.disk_tags':dict(type='Microsoft.Compute/disks@2024-03-02',resource_id=arm+'Microsoft.Compute/disks/os-'+suffix,body={'tags':tags('0.505')})}
    else:
        account=arm+'Microsoft.Storage/storageAccounts/pwtest0001'
        values={
            'azurerm_resource_group.project':dict(name='rg-pathwatch-lab',location='westus',tags=tags('0.085')),
            'azurerm_storage_account.state':dict(name='pwtest0001',resource_group_name='rg-pathwatch-lab',location='westus',account_tier='Standard',account_replication_type='LRS',account_kind='StorageV2',min_tls_version='TLS1_2',https_traffic_only_enabled=True,shared_access_key_enabled=False,default_to_oauth_authentication=True,allow_nested_items_to_be_public=False,public_network_access_enabled=True,cross_tenant_replication_enabled=False,tags=tags('0.085'),network_rules=[dict(default_action='Deny',bypass=['None'],ip_rules=['8.8.8.8'])],blob_properties=[dict(versioning_enabled=True,change_feed_enabled=True,change_feed_retention_in_days=7,delete_retention_policy=[{'days':7}],container_delete_retention_policy=[{'days':7}])]),
            'azurerm_role_assignment.operator_state':dict(scope=account,role_definition_name='Storage Blob Data Contributor',principal_id='22222222-2222-2222-2222-222222222222'),
            'azurerm_storage_container.state':dict(name='tfstate',storage_account_id=account,container_access_type='private')}
    providers={'azurerm':dict(full_name='registry.terraform.io/hashicorp/azurerm',expressions={'subscription_id':{'references':['var.subscription_id']},'resource_provider_registrations':{'constant_value':'none'},'features':{'constant_value':[{'virtual_machine':[{'delete_os_disk_on_deletion':True}]}] if kind=='xdp-runner' else [{}]}})}
    if kind=='xdp-runner':providers['azapi']=dict(full_name='registry.terraform.io/azure/azapi',expressions={'subscription_id':{'references':['var.subscription_id']},'skip_provider_registration':{'constant_value':True},'enable_preflight':{'constant_value':False}})
    else:providers['azurerm']['expressions']['storage_use_azuread']={'constant_value':True}
    variables={k:{'value':v} for k,v in dict(subscription_id=SUB,location='westus',ttl=END).items()}
    if kind=='xdp-runner':variables.update(xdp_scope={'value':kind},run_id={'value':'test0001'})
    return dict(variables=variables,configuration={'provider_config':providers,'root_module':{'resources':[dict(address=a,expressions={}) for a in values]}},resource_changes=[dict(address=a,type=a.split('.')[0],mode='managed',provider_name='registry.terraform.io/'+('azure/azapi' if a.startswith('azapi') else 'hashicorp/azurerm'),change=dict(actions=['create'],before=None,after=v,after_unknown={'id':True})) for a,v in values.items()])

def value(plan,address):return next(r['change']['after'] for r in plan['resource_changes'] if r['address']==address)
def private(path,data):
    path.write_bytes(json.dumps(data).encode());path.chmod(0o600);return path

def deleting(plan,ctx):
    result=copy.deepcopy(plan)
    for r in result['resource_changes']:
        v=r['change']['after'];rid=scope.resource_id(r['address'],v,ctx)
        if rid is None:rid=v['scope']+'/providers/Microsoft.Authorization/roleAssignments/33333333-3333-3333-3333-333333333333'
        v['id']=rid;r['change']=dict(actions=['delete'],before=v,after=None,after_unknown={})
    return result

class ScopeTests(unittest.TestCase):
    def test_bootstrap_manifest_and_extra_provider_inputs_are_bound(self):
        ctx=context('xdp-bootstrap');del ctx['source_hashes']
        with self.assertRaisesRegex(ValueError,'manifest'):scope.validate(fixture('xdp-bootstrap'),SUB,ctx)
        ctx['source_hashes']=dict(life.BOOTSTRAP_HASHES)
        self.assertEqual(len(scope.validate(fixture('xdp-bootstrap'),SUB,ctx)),4)
        ctx['source_hashes']['main.tf']='0'*64
        with self.assertRaisesRegex(ValueError,'manifest'):scope.validate(fixture('xdp-bootstrap'),SUB,ctx)
        p=fixture();p['configuration']['provider_config']['azurerm']['expressions']['storage_use_azuread']={'constant_value':False}
        with self.assertRaisesRegex(ValueError,'Provider'):scope.validate(p,SUB,context())
    def test_two_exact_synthetic_roots_and_known_computed_outputs(self):
        for kind,count in [('xdp-runner',8),('xdp-bootstrap',4)]:
            p=fixture(kind);self.assertEqual(len(scope.validate(p,SUB,context(kind))),count)
        p=fixture();p['resource_changes'][6]['change']['after_unknown']['virtual_machine_id']=True
        scope.validate(p,SUB,context())
    def test_known_nested_unknown_masks_accept_only_false_leaves(self):
        p=fixture();r=p['resource_changes'][-1]
        r['change']['after_unknown']={'id':True,'body':{'tags':{'ttl':False}}}
        self.assertEqual(len(scope.validate(p,SUB,context())),8)
        r['change']['after_unknown']['body']['tags']['ttl']=True
        with self.assertRaises(ValueError):scope.validate(p,SUB,context())
    def test_missing_extra_mixed_duplicate_and_provider_mutations(self):
        for mutation in ['missing','extra','duplicate','provider','registration','delete_disk','alias']:
            p=fixture()
            if mutation=='missing':p['resource_changes'].pop()
            elif mutation=='extra':p['resource_changes'].append(dict(address='azurerm_resource_group.project',type='azurerm_resource_group',mode='managed'))
            elif mutation=='duplicate':p['resource_changes'].append(copy.deepcopy(p['resource_changes'][0]))
            elif mutation=='provider':p['resource_changes'][0]['provider_name']='evil/provider'
            elif mutation=='registration':p['configuration']['provider_config']['azapi']['expressions']['skip_provider_registration']={'constant_value':False}
            elif mutation=='delete_disk':p['configuration']['provider_config']['azurerm']['expressions']['features']={'constant_value':[{}]}
            else:p['configuration']['provider_config']['azurerm']['alias']='foreign'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):scope.validate(p,SUB,context())
    def test_critical_unknown_and_write_only_inputs_refuse(self):
        for address,field in [('azurerm_linux_virtual_machine.runner','size'),('azapi_update_resource.disk_tags','sensitive_body'),('azurerm_public_ip.runner','public_ip_prefix_id'),('azurerm_network_security_group.runner','security_rule')]:
            p=fixture();r=next(r for r in p['resource_changes'] if r['address']==address);r['change']['after_unknown'][field]=True
            with self.subTest(field=field),self.assertRaises(ValueError):scope.validate(p,SUB,context())
        p=fixture();p['configuration']['root_module']['resources'][-1]['expressions']['sensitive_body']={'constant_value':None}
        with self.assertRaises(ValueError):scope.validate(p,SUB,context())
    def test_policy_and_ownership_mutations_refuse(self):
        cases=[('azurerm_linux_virtual_machine.runner','size','Standard_D2as_v5'),('azurerm_linux_virtual_machine.runner','size','Standard_B2s'),('azurerm_linux_virtual_machine.runner','identity',[{}]),('azurerm_linux_virtual_machine.runner','boot_diagnostics',[{}]),('azurerm_network_interface.runner','accelerated_networking_enabled',True),('azapi_update_resource.disk_tags','body',{'properties':{}})]
        for address,field,replacement in cases:
            p=fixture();value(p,address)[field]=replacement
            with self.subTest(field=field,replacement=replacement),self.assertRaises(ValueError):scope.validate(p,SUB,context())
        p=fixture();value(p,'azurerm_virtual_network.runner')['tags']['owner']='foreign'
        with self.assertRaises(ValueError):scope.validate(p,SUB,context())
        p=fixture();value(p,'azurerm_network_security_group.runner')['security_rule'][0]['destination_port_range']='*'
        with self.assertRaises(ValueError):scope.validate(p,SUB,context())
        for ttl in ['2026-10-25T03:00:00Z','bad']:
            ctx=context();ctx['ttl']=ttl
            with self.assertRaises(ValueError):scope.validate(fixture(),SUB,ctx)

class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name);self.api=SimpleNamespace(**vars(budget));self.api.ROOT=self.path
        self.pricing={}
        prices={'vm_hours':'.08','ip_hours':'.005','disk_month':'1.536','disk_transactions':'.0005','aggregate_egress_gb':'.087','backend_gb_month':'.0208','backend_write_10k':'.055','backend_read_10k':'.0044','backend_list_10k':'.055','backend_other_10k':'.0044'}
        # Explicit synthetic primary API shape; no network/real-price freshness claim.
        for category,(product,sku,meter,unit,quantity) in budget.xdp_budget.METERS.items():
            selector=dict(meterId=category,skuId=category,armRegionName='westus',unitOfMeasure=unit,productName=product,skuName=sku,meterName=meter,tierMinimumUnits=0)
            raw=dict(captured_at=START,url='https://prices.azure.com/api/retail/prices?synthetic',response={'Items':[dict(selector,currencyCode='USD',type='Consumption',effectiveStartDate='2026-01-01T00:00:00Z',retailPrice=prices[category])],'NextPageLink':None})
            p=self.path/(category+'.json');p.write_text(json.dumps(raw));self.pricing[category]=dict(capture=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),selector=selector,quantity=str(quantity))
    def proposal(self,kind='xdp-runner'):
        ctx=context(kind);cap=dict(subscription_id=SUB,location='westus',captured_at=START,allowed_regions=['westus'],sku=dict(name='Standard_B2ps_v2',restrictions=[],location='westus',architecture='Arm64',vcpus=2,memory_gib=8),quota={'cores':dict(current=0,limit=6),'standardBpsv2Family':dict(current=0,limit=10)},image=dict(urn='Canonical:ubuntu-24_04-lts:server-arm64:24.04.202610210',architecture='Arm64',hyperv='V2',plan=None,minimum_disk_gib=30))
        assigned={'azurerm_storage_account.state':(['backend_gb_month','backend_list_10k','backend_other_10k','backend_read_10k','backend_write_10k'],'0.17'),'azurerm_linux_virtual_machine.runner':(['aggregate_egress_gb','vm_hours'],'0.34'),'azurerm_public_ip.runner':(['ip_hours'],'0.01'),'azapi_update_resource.disk_tags':(['disk_month','disk_transactions'],'1.01')}
        rows=[]
        for r in fixture(kind)['resource_changes']:
            cats,cost=assigned.get(r['address'],([], '0'))
            rows.append(dict(address=r['address'],type=r['type'],region='westus',start_utc=START,end_utc=END,same_session_delete=True,run_id='test0001',categories=cats,estimate_usd=cost))
        return dict(scope=kind,context=ctx,capability=cap,pricing=copy.deepcopy(self.pricing),resources=rows)
    def evaluate(self,p,kind='xdp-runner',ledger=None):return budget.xdp_budget.evaluate(fixture(kind),p,ledger or {'resources':[]},NOW,self.api)
    def test_complete_original_10k_campaign_and_both_scopes(self):
        for kind in ['xdp-bootstrap','xdp-runner']:
            r=self.evaluate(self.proposal(kind),kind);self.assertEqual(r['campaign_usd'],'1.53');self.assertFalse(r['live_admission'])
            self.assertEqual(len(r['category_costs']),10)
    def test_missing_disk_cost_reduced_bounds_wrong_unit_nonfinite_refuse(self):
        for mutation in ['missing','quantity','unit','duplicate','underprice','nan']:
            p=self.proposal()
            if mutation=='missing':del p['pricing']['disk_transactions']
            elif mutation=='quantity':p['pricing']['backend_write_10k']['quantity']='0.1'
            elif mutation=='unit':p['pricing']['vm_hours']['selector']['unitOfMeasure']='1 Month'
            elif mutation=='duplicate':p['resources'].append(copy.deepcopy(p['resources'][-1]))
            elif mutation=='underprice':p['resources'][-1]['estimate_usd']='0.01'
            else:p['pricing']['vm_hours']['quantity']='NaN'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.evaluate(p)
    def test_changed_stale_capture_and_historical_cap(self):
        p=self.proposal();path=self.path/'vm_hours.json';raw=json.loads(path.read_text());raw['captured_at']='2026-10-20T00:00:00Z';path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError,'changed'):self.evaluate(p)
        p['pricing']['vm_hours']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'stale'):self.evaluate(p)
        p=self.proposal();raw['captured_at']=START;path.write_text(json.dumps(raw));p['pricing']['vm_hours']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'18 USD'):self.evaluate(p,ledger={'resources':[{'estimate_usd':'17','status':'deleted'}]})
    def test_unknown_image_quota_restrictions_and_region_refuse(self):
        for field in ['image','quota','sku','region']:
            p=self.proposal()
            if field=='image':del p['capability']['image']['minimum_disk_gib']
            elif field=='quota':p['capability']['quota']['standardBpsv2Family']['limit']=0
            elif field=='sku':p['capability']['sku']['restrictions']=[{'reasonCode':'NotAvailableForSubscription'}]
            else:p['capability']['allowed_regions']=[]
            with self.subTest(field=field),self.assertRaises(ValueError):self.evaluate(p)
    def test_actual_dispatch_and_locked_reserve_are_always_pending(self):
        p=self.proposal();p.update(approved=True,synthetic=True)
        ledger=self.path/'ledger.json';ledger.write_text(json.dumps({'resources':[]}));before=ledger.read_bytes()
        with patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB),patch.object(budget,'LEDGER',ledger):
            with self.assertRaisesRegex(ValueError,'PENDING'):budget.check(fixture(),p,{'resources':[]},NOW)
            with self.assertRaisesRegex(ValueError,'PENDING'):budget.admit('reserve',fixture(),p,'a'*64)
        self.assertEqual(ledger.read_bytes(),before)

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name);self.path.chmod(0o700)
    def test_private_create_only_manifest_bound_bootstrap(self):
        ctx=context('xdp-bootstrap');target=life.prepare(self.path,ctx)
        self.assertFalse((target/'terraform.tfstate').exists());self.assertEqual((target.stat().st_mode&0o777),0o700)
        for name in life.BOOTSTRAP_FILES:
            self.assertEqual((target/name).read_bytes(),(ROOT/'infra/bootstrap'/name).read_bytes());self.assertEqual((target/name).stat().st_mode&0o777,0o600)
        receipt=json.loads((target/'run.json').read_text());self.assertNotEqual(receipt['bootstrap_key'],receipt['runner_key'])
        with self.assertRaises(FileExistsError):life.prepare(self.path,ctx)
        ctx['run_id']='different1'
        with patch.dict(life.BOOTSTRAP_HASHES,{'main.tf':'0'*64}):
            with self.assertRaisesRegex(ValueError,'manifest'):life.prepare(self.path,ctx)
        self.assertFalse((self.path/'different1').exists())
    def export_receipt(self):
        ctx=context('xdp-bootstrap');plan=deleting(fixture('xdp-bootstrap'),ctx)
        resources=[dict(mode='managed',type=r['type'],name=r['address'].split('.')[1],instances=[{'attributes':r['change']['before']}]) for r in plan['resource_changes']]
        exports={}
        for name,lineage,items in [('bootstrap_remote','44444444-4444-4444-4444-444444444444',resources),('bootstrap_local','44444444-4444-4444-4444-444444444444',resources),('runner_empty','55555555-5555-5555-5555-555555555555',[])]:
            path=private(self.path/(name+'.json'),dict(version=4,lineage=lineage,serial=3,resources=items));exports[name]=dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return plan,dict(scope='xdp-bootstrap',context=ctx,exports=exports)
    def test_backend_teardown_requires_bound_durable_export_and_migration(self):
        plan,receipt=self.export_receipt()
        self.assertTrue(teardown.validate(plan,SUB,receipt)['offline_only'])
        with self.assertRaises(ValueError):teardown.validate(plan,SUB)
        missing=copy.deepcopy(receipt);del missing['exports']
        with self.assertRaisesRegex(ValueError,'exports'):teardown.validate(plan,SUB,missing)
        local=Path(receipt['exports']['bootstrap_local']['path']);data=json.loads(local.read_text());data['serial']=4;private(local,data);receipt['exports']['bootstrap_local']['sha256']=hashlib.sha256(local.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'lineage/serial/count'):teardown.validate(plan,SUB,receipt)
    def test_migration_rejects_different_generated_role_identities(self):
        plan,receipt=self.export_receipt()
        local=Path(receipt['exports']['bootstrap_local']['path']);data=json.loads(local.read_text())
        role=next(r for r in data['resources'] if r['type']=='azurerm_role_assignment')
        role['instances'][0]['attributes']['id']=role['instances'][0]['attributes']['id'].replace('33333333','66666666')
        private(local,data);receipt['exports']['bootstrap_local']['sha256']=hashlib.sha256(local.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'identit'):life.verify_migration(receipt)
    def test_teardown_rejects_role_identity_different_from_both_exports(self):
        plan,receipt=self.export_receipt()
        role=next(r for r in plan['resource_changes'] if r['type']=='azurerm_role_assignment')
        role['change']['before']['id']=role['change']['before']['id'].replace('33333333','66666666')
        with self.assertRaisesRegex(ValueError,'identit'):teardown.validate(plan,SUB,receipt)
    def test_foreign_exports_same_count_are_not_migration_evidence(self):
        plan,receipt=self.export_receipt()
        for name in ['bootstrap_remote','bootstrap_local']:
            path=Path(receipt['exports'][name]['path']);data=json.loads(path.read_text());data['resources'][0]['instances'][0]['attributes']['tags']['owner']='foreign';private(path,data);receipt['exports'][name]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):teardown.validate(plan,SUB,receipt)
    def test_bootstrap_cleanup_role_is_bound_to_private_export(self):
        plan,receipt=self.export_receipt()
        role=next(r['change']['before'] for r in plan['resource_changes'] if r['type']=='azurerm_role_assignment')
        inventory=[{'id':role['id']}]
        self.assertEqual(life.cleanup_candidates(receipt,inventory,budget.utc(END))['candidate_ids'],[role['id']])
        inventory[0]['id']=role['id'].replace('33333333','66666666')
        with self.assertRaises(ValueError):life.cleanup_candidates(receipt,inventory,budget.utc(END))
    def test_private_receipts_reject_links_modes_and_hash_changes(self):
        path=private(self.path/'receipt.json',{'a':1});path.chmod(0o644)
        with self.assertRaises(ValueError):life.read_private(path)
        path.chmod(0o600);link=self.path/'link.json';link.symlink_to(path)
        with self.assertRaises(OSError):life.read_private(link)
        hard=self.path/'hard.json';os.link(path,hard)
        with self.assertRaises(ValueError):life.read_private(path)
    def test_partial_apply_untagged_disk_requires_owned_vm_storage_profile(self):
        ctx=context();plan=deleting(fixture(),ctx);vm=next(r['change']['before'] for r in plan['resource_changes'] if r['type']=='azurerm_linux_virtual_machine');disk=next(r['change']['before'] for r in plan['resource_changes'] if r['type']=='azapi_update_resource');inventory=[{'id':vm['id'],'tags':vm['tags'],'storageProfile':{'osDisk':{'managedDisk':{'id':disk['id']}}}},{'id':disk['id'],'tags':{}}];receipt=dict(scope='xdp-runner',context=ctx)
        result=life.cleanup_candidates(receipt,inventory,budget.utc(END));self.assertEqual(len(result['candidate_ids']),2);self.assertTrue(result['offline_only'])
        with self.assertRaises(ValueError):life.cleanup_candidates(receipt,inventory,NOW)
        for bad in [inventory[1:],inventory+[{'id':'/subscriptions/foreign/resourceGroups/rg-mlinfra-westus','tags':{}}]]:
            with self.assertRaises(ValueError):life.cleanup_candidates(receipt,bad,budget.utc(END))
        inventory[0]['storageProfile']['osDisk']['managedDisk']['id']='different'
        with self.assertRaises(ValueError):life.cleanup_candidates(receipt,inventory,budget.utc(END))
    def test_real_cli_render_boundary_binds_binary_and_never_invokes_azure(self):
        plan=deleting(fixture(),context());binary=self.path/'reviewed.tfplan';binary.write_bytes(b'synthetic-binary');payload=self.path/'plan.json';payload.write_text(json.dumps(plan));digest=hashlib.sha256(binary.read_bytes()).hexdigest();fake=self.path/'terraform'
        fake.write_text('#!/usr/bin/env python3\nimport os,pathlib,sys\nassert sys.argv[1:3]==["show","-json"]\nprint(pathlib.Path(os.environ["XDP_TEST_PLAN"]).read_text())\nif os.environ.get("XDP_TEST_CHANGE"):pathlib.Path(sys.argv[3]).write_bytes(b"changed")\n');fake.chmod(0o700)
        receipt=private(self.path/'receipt.json',dict(scope='xdp-runner',context=context()));env=os.environ|{'PATH':str(self.path)+os.pathsep+os.environ['PATH'],'XDP_TEST_PLAN':str(payload),'AZURE_SUBSCRIPTION_ID':SUB}
        result=subprocess.run([sys.executable,str(ROOT/'scripts/azure_teardown.py'),'--plan',str(binary),'--reviewed-sha256',digest,'--xdp-receipt',str(receipt)],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('OFFLINE',result.stdout)
        result=subprocess.run([sys.executable,str(ROOT/'scripts/azure_teardown.py'),'--plan',str(binary),'--reviewed-sha256',digest,'--xdp-receipt',str(receipt)],env=env|{'XDP_TEST_CHANGE':'1'},capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('changed',result.stderr)
        # Extra receipt arguments cannot route through ordinary down and call az.
        result=subprocess.run([sys.executable,str(ROOT/'scripts/azure_reaper.py'),'down','--xdp-receipt',str(receipt)],env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
    def test_reaper_xdp_check_is_offline_and_exportless_bootstrap_refuses(self):
        receipt=private(self.path/'receipt.json',dict(scope='xdp-bootstrap',context=context('xdp-bootstrap')));inventory=private(self.path/'inventory.json',[])
        command=[sys.executable,str(ROOT/'scripts/azure_reaper.py'),'xdp-check','--receipt',str(receipt),'--inventory',str(inventory)]
        result=subprocess.run(command,capture_output=True,text=True);self.assertNotEqual(result.returncode,0)
        private(receipt,dict(scope='xdp-runner',context=context()));result=subprocess.run(command,capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr);self.assertIn('PENDING',result.stdout)

class LegacyBoundaryTests(unittest.TestCase):
    def test_actual_legacy_bootstrap_cap_and_old_down_refusal(self):
        from test_azure_controls import PlanBudgetTests
        case=PlanBudgetTests('test_valid_bounded_real_capture');case.setUp()
        try:
            case.check()
            row=case.plan['resource_changes'][0]['change']['after'];row['tags']['est_usd_per_hour']='0.03';case.proposal['resources'][0]['same_session_delete']=True
            with self.assertRaisesRegex(ValueError,'Ordinary bootstrap'):case.check()
        finally:case.doCleanups()
        result=subprocess.run(['make','-C',str(ROOT/'infra/bootstrap'),'down'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('REFUSED',result.stderr)

if __name__=='__main__':unittest.main()
