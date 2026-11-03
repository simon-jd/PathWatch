"""Exact synthetic binary-plan shapes; no Azure calls or paid resources."""
import copy
import datetime as dt
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_azure_network_controls as networktests
SUB=networktests.SUB
PREFIX=networktests.PREFIX
ROOT=Path(__file__).resolve().parents[2]
def load(name):
    spec=importlib.util.spec_from_file_location(name,Path(os.environ.get('NVA_TEST_SOURCE',ROOT/'scripts'))/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
budget=load('azure_budget');teardown=load('azure_teardown')
SELECT_KEYS=('meterId','skuId','armRegionName','unitOfMeasure','productName','skuName','meterName','tierMinimumUnits')
class NvaControlsTests(unittest.TestCase):
    def setUp(self):
        self.base=networktests.NetworkControlsTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.plan=self.base.plan;self.proposal=self.base.proposal;self.now=self.base.now;self.end=self.base.end
        self.prefix=f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab/providers/'
        tags={'project':'pathwatch','owner':'codex-pathwatch','ttl':self.end,'est_usd_per_hour':'0.1'}
        self.plan['resource_changes'].append({'mode':'data','type':'azurerm_resource_group','address':'module.network.data.azurerm_resource_group.project','change':{'after':{'id':f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab','tags':tags}}})
        self.plan['variables']['ttl']={'value':self.end}
        for row in self.proposal['resources']:row['same_session_delete']=True
        self.plan['variables']['owner_peer_cidrs']={'value':['8.8.8.8/32']}
        for suffix,port,priority in [('wireguard','51820',2000),('wireguard_alternate','51821',2200)]:
            name='allow-wireguard-'+('alternate-' if suffix.endswith('alternate') else '')+'0'
            address=f'module.network.azurerm_network_security_rule.{suffix}["8.8.8.8/32"]'
            value={'name':name,'resource_group_name':'rg-pathwatch-lab','network_security_group_name':'nsg-pathwatch-azure-a','direction':'Inbound','access':'Allow','priority':priority,'protocol':'Udp','source_address_prefix':'8.8.8.8/32','destination_address_prefix':'10.10.1.0/27','source_port_range':'*','destination_port_range':port,'id':PREFIX+'networkSecurityGroups/nsg-pathwatch-azure-a/securityRules/'+name}
            self.plan['resource_changes'].append({'mode':'managed','type':'azurerm_network_security_rule','address':address,'change':{'actions':['create'],'after':value}})
            self.proposal['resources'].append(self.proposal['resources'][0]|{'address':address,'type':'azurerm_network_security_rule'})
        for key,protocol,port in [('icmp','Icmp','*'),('iperf','Tcp','5201')]:
            name='allow-hybrid-forward-'+key;address=f'module.network.azurerm_network_security_rule.hybrid_forwarding["{key}"]'
            value={'name':name,'resource_group_name':'rg-pathwatch-lab','network_security_group_name':'nsg-pathwatch-azure-a','direction':'Inbound','access':'Allow','priority':2600,'protocol':protocol,'source_address_prefix':'10.10.0.0/24','destination_address_prefix':'10.30.0.0/16','source_port_range':'*','destination_port_range':port,'id':PREFIX+'networkSecurityGroups/nsg-pathwatch-azure-a/securityRules/'+name}
            self.plan['resource_changes'].append({'mode':'managed','type':'azurerm_network_security_rule','address':address,'change':{'actions':['create'],'after':value}})
            self.proposal['resources'].append(self.proposal['resources'][0]|{'address':address,'type':'azurerm_network_security_rule'})
        for index, (key,protocol,port) in enumerate([('icmp','Icmp','*'),('iperf','Tcp','5201')]):
            name='allow-hybrid-outbound-'+key;address=f'module.network.azurerm_network_security_rule.hybrid_outbound["{key}"]'
            value={'name':name,'resource_group_name':'rg-pathwatch-lab','network_security_group_name':'nsg-pathwatch-azure-a','direction':'Outbound','access':'Allow','priority':2600+index,'protocol':protocol,'source_address_prefix':'10.30.0.0/16','destination_address_prefix':'10.10.0.0/24','source_port_range':'*','destination_port_range':port,'id':PREFIX+'networkSecurityGroups/nsg-pathwatch-azure-a/securityRules/'+name}
            self.plan['resource_changes'].append({'mode':'managed','type':'azurerm_network_security_rule','address':address,'change':{'actions':['create'],'after':value}})
            self.proposal['resources'].append(self.proposal['resources'][0]|{'address':address,'type':'azurerm_network_security_rule'})
        for key,cidr in [('home','10.30.0.0/16'),('wireguard','10.255.0.0/24')]:
            address=f'module.network.azurerm_route.home["{key}"]';value={'name':'to-'+key,'resource_group_name':'rg-pathwatch-lab','route_table_name':'rt-pathwatch-azure-a','address_prefix':cidr,'next_hop_type':'VirtualAppliance','next_hop_in_ip_address':'10.10.1.4','id':PREFIX+'routeTables/rt-pathwatch-azure-a/routes/to-'+key}
            self.plan['resource_changes'].append({'mode':'managed','type':'azurerm_route','address':address,'change':{'actions':['create'],'after':value}})
            self.proposal['resources'].append(self.proposal['resources'][0]|{'address':address,'type':'azurerm_route'})
        images={'publisher':'Canonical','offer':'ubuntu-24_04-lts','sku':'server-arm64','version':'24.04.202610210'}
        values={
          'azurerm_public_ip':{'name':'pip-pathwatch-hybrid-nva','domain_name_label':'pathwatch-'+hashlib.sha256(SUB.encode()).hexdigest()[:10]+'-nva','allocation_method':'Static','sku':'Standard','sku_tier':'Regional','ip_version':'IPv4','id':self.prefix+'Microsoft.Network/publicIPAddresses/pip-pathwatch-hybrid-nva'},
          'azurerm_network_interface':{'name':'nic-pathwatch-hybrid-nva','ip_forwarding_enabled':True,'accelerated_networking_enabled':False,'ip_configuration':[{'name':'primary','primary':True,'subnet_id':PREFIX+'virtualNetworks/vnet-pathwatch-azure-a/subnets/snet-nva','private_ip_address_allocation':'Static','private_ip_address':'10.10.1.4','public_ip_address_id':self.prefix+'Microsoft.Network/publicIPAddresses/pip-pathwatch-hybrid-nva'}],'id':self.prefix+'Microsoft.Network/networkInterfaces/nic-pathwatch-hybrid-nva'},
          'azurerm_linux_virtual_machine':{'name':'vm-pathwatch-hybrid-nva','size':'Standard_B2pts_v2','admin_username':'pathwatch','disable_password_authentication':True,'allow_extension_operations':False,'source_image_reference':[images],'os_disk':[{'name':'os-pathwatch-hybrid-nva','storage_account_type':'Standard_LRS','disk_size_gb':30}],'network_interface_ids':[self.prefix+'Microsoft.Network/networkInterfaces/nic-pathwatch-hybrid-nva'],'id':self.prefix+'Microsoft.Compute/virtualMachines/vm-pathwatch-hybrid-nva'},
          'azapi_update_resource':{'type':'Microsoft.Compute/disks@2024-03-02','resource_id':self.prefix+'Microsoft.Compute/disks/os-pathwatch-hybrid-nva','body':{'tags':tags},'id':self.prefix+'Microsoft.Compute/disks/os-pathwatch-hybrid-nva'},
        }
        for typ,value in values.items():
            value.update(location='westus',resource_group_name='rg-pathwatch-lab',tags=tags.copy())
            if typ=='azurerm_network_interface':value['tags']['est_usd_per_hour']='0'
            address=f'module.appliance.{typ}.{"disk_tags" if typ=="azapi_update_resource" else "this"}["nva"]'
            self.plan['resource_changes'].append({'mode':'managed','type':typ,'address':address,'change':{'actions':['create'],'after':value,'after_unknown':{}}})
            hours=budget.money(str((budget.utc(self.end)-budget.utc(self.proposal['resources'][0]['start_utc'])).total_seconds()))/budget.money(3600)
            categories={'azurerm_linux_virtual_machine':{'vm_hours':str(hours),'egress_gb':'2'},'azurerm_public_ip':{'ip_hours':str(hours)},'azapi_update_resource':{'disk_month':str(hours/budget.money(730)),'transactions':'1000000'},'azurerm_network_interface':{}}[typ]
            row={'address':address,'type':typ,'region':'westus','start_utc':self.proposal['resources'][0]['start_utc'],'end_utc':self.end,'estimate_usd':'0' if typ=='azurerm_network_interface' else '.5','same_session_delete':True,'bounds':categories,'meters':[]}
            for category in categories:
                capture,product,meter={
                  'vm_hours':('vm-b2pts','Virtual Machines Bpsv2 Series','B2pts v2'),
                  'egress_gb':('bandwidth','Rtn Preference: MGN','Standard Data Transfer Out'),
                  'ip_hours':('network','IP Addresses','Standard IPv4 Static Public IP'),
                  'disk_month':('hdd-s4','Standard HDD Managed Disks','S4 LRS Disk'),
                  'transactions':('hdd-s4','Standard HDD Managed Disks','S4 LRS Disk Operations')}[category]
                path=f'docs/azure-prices/2026-10-23-{capture}-westus.json'
                evidence=json.loads((ROOT/path).read_text());self.now=max(self.now,budget.utc(evidence['captured_at'])+dt.timedelta(seconds=1))
                matches=[x for x in evidence['response']['Items'] if x['productName']==product and x['meterName']==meter and x['type']=='Consumption' and x['retailPrice']>0]
                item=max(matches,key=lambda x:x['retailPrice'])
                row['meters'].append({'category':category,'capture':path,'selector':{k:item[k] for k in SELECT_KEYS}})
            self.proposal['resources'].append(row)
    def check(self):return budget.check(self.plan,self.proposal,{'resources':[{'estimate_usd':'.30'}]},self.now)
    def r(self,typ):return next(x for x in self.plan['resource_changes'] if x.get('type')==typ)['change']['after']
    def row(self,typ):return next(x for x in self.proposal['resources'] if x['type']==typ)
    def test_accept_owned_priced_nva_and_explicit_disk_tags(self):self.check()
    def move_parent_to(self,container):
        resource=next(r for r in self.plan['resource_changes'] if r.get('mode')=='data')
        self.plan['resource_changes'].remove(resource)
        item={'address':resource['address'],'mode':'data','type':'azurerm_resource_group','values':resource['change']['after']}
        module={'resources':[item],'child_modules':[]}
        root={'child_modules':[{'address':'module.network','resources':[], 'child_modules':[module]}]}
        if container=='prior_state':self.plan['prior_state']={'values':{'root_module':root}}
        else:self.plan['planned_values']={'root_module':root}
        return item
    def test_parent_rg_in_real_prior_state_nested_shape_is_accepted(self):
        self.move_parent_to('prior_state');self.check()
    def test_parent_rg_in_planned_values_remains_accepted(self):
        self.move_parent_to('planned_values');self.check()
    def test_prior_parent_foreign_missing_expired_or_malformed_ttl_refused(self):
        original=copy.deepcopy(self.plan)
        for case in ('foreign_id','foreign_owner','missing','expired','malformed_ttl','wrong_type','duplicate'):
            self.plan=copy.deepcopy(original);item=self.move_parent_to('prior_state')
            if case=='foreign_id':item['values']['id']=item['values']['id'].replace('rg-pathwatch-lab','rg-mlinfra-westus')
            elif case=='foreign_owner':item['values']['tags']['owner']='codex-warden'
            elif case=='missing':self.plan.pop('prior_state')
            elif case=='expired':item['values']['tags']['ttl']='2000-01-01T00:00:00Z'
            elif case=='malformed_ttl':item['values']['tags']['ttl']='9999-99-99T99:99:99Z'
            elif case=='wrong_type':item['type']='azurerm_storage_account'
            else:self.plan['prior_state']['values']['root_module']['resources']=[copy.deepcopy(item)]
            with self.subTest(case=case),self.assertRaises(ValueError):self.check()
        self.plan=original
    def test_current_or_planned_parent_does_not_read_older_invalid_state(self):
        original=copy.deepcopy(self.plan)
        for container in ('resource_changes','planned_values'):
            self.plan=copy.deepcopy(original)
            if container=='planned_values':self.move_parent_to('planned_values')
            self.plan['prior_state']={'values':{'root_module':None}}
            self.check()
        self.plan=original
    def test_invalid_current_parent_cannot_fall_back_to_old_valid_prior(self):
        original=copy.deepcopy(self.plan)
        for container in ('resource_changes','planned_values'):
            for invalid in (None,{}, {'id':'foreign','tags':{'project':'pathwatch','owner':'codex-pathwatch','ttl':self.end}}):
                self.plan=copy.deepcopy(original);item=self.move_parent_to('prior_state')
                current={'address':item['address'],'mode':'data','type':'azurerm_resource_group'}
                if container=='resource_changes':
                    current['change']={'actions':['read'],'after':invalid,'after_unknown':{'id':True}}
                    self.plan['resource_changes'].append(current)
                else:
                    current['values']=invalid;self.plan['planned_values']={'root_module':{'resources':[current]}}
                with self.subTest(container=container,invalid=invalid),self.assertRaises(ValueError):self.check()
        self.plan=original

    def test_refuse_sku_disk_image_forwarding_and_global_pip(self):
        changes=[('azurerm_linux_virtual_machine','size','Standard_NC6'),('azurerm_public_ip','sku_tier','Global'),('azurerm_network_interface','ip_forwarding_enabled',False)]
        for typ,key,value in changes:
            saved=copy.deepcopy(self.plan);self.r(typ)[key]=value
            with self.subTest(typ=typ),self.assertRaises(ValueError):self.check()
            self.plan=saved
        self.r('azurerm_linux_virtual_machine')['os_disk'][0]['disk_size_gb']=64
        with self.assertRaises(ValueError):self.check()
    def test_disk_patch_cannot_escape_owned_disk_or_mutate_properties(self):
        for field,value in [('resource_id',self.prefix+'Microsoft.Compute/disks/foreign'),('body',{'properties':{'diskSizeGB':1024}})]:
            saved=copy.deepcopy(self.plan);self.r('azapi_update_resource')[field]=value
            with self.assertRaises(ValueError):self.check()
            self.plan=saved
    def test_transit_nsg_cannot_open_other_destination_protocol_or_port(self):
        for field,value in [('destination_address_prefix','0.0.0.0/0'),('source_address_prefix','0.0.0.0/0'),('destination_port_range','*'),('protocol','Udp')]:
            saved=copy.deepcopy(self.plan)
            resource=next(r for r in self.plan['resource_changes'] if '.hybrid_forwarding["iperf"]' in r['address'])
            resource['change']['after'][field]=value
            with self.assertRaises(ValueError):self.check()
            self.plan=saved
    def test_missing_alternate_or_udr_refused(self):
        for typ in ('azurerm_route','azurerm_network_security_rule'):
            saved=copy.deepcopy(self.plan)
            self.plan['resource_changes']=[r for r in self.plan['resource_changes'] if not (r.get('type')==typ and ('.home[' in r['address'] or '.wireguard_alternate[' in r['address']))]
            with self.assertRaises(ValueError):self.check()
            self.plan=saved
    def test_disk_tag_manager_cannot_be_omitted(self):
        self.plan['resource_changes']=[r for r in self.plan['resource_changes'] if r.get('type')!='azapi_update_resource']
        self.proposal['resources']=[r for r in self.proposal['resources'] if r['type']!='azapi_update_resource']
        with self.assertRaises(ValueError):self.check()
    def test_every_priced_category_and_accurate_quantity_required(self):
        for typ in ('azurerm_linux_virtual_machine','azapi_update_resource','azurerm_public_ip'):
            saved=copy.deepcopy(self.proposal);self.row(typ)['meters'].pop()
            with self.assertRaises(ValueError):self.check()
            self.proposal=saved
        self.row('azurerm_public_ip')['bounds']['ip_hours']='0.1'
        with self.assertRaises(ValueError):self.check()
    def test_meter_sku_unit_and_underestimate_refused(self):
        row=self.row('azapi_update_resource');row['estimate_usd']='.000001'
        with self.assertRaises(ValueError):self.check()
    def test_cross_parent_expiry_refused(self):
        self.r('azapi_update_resource')['body']['tags']['ttl']='2027-01-01T00:00:00Z'
        with self.assertRaises(ValueError):self.check()
    def test_no_update_replacement_or_foreign_type(self):
        for actions in [['update'],['delete','create']]:
            saved=copy.deepcopy(self.plan);next(r for r in self.plan['resource_changes'] if r.get('type')=='azapi_update_resource')['change']['actions']=actions
            with self.assertRaises(ValueError):self.check()
            self.plan=saved
    def test_disk_timebox_cannot_be_shorter_than_parent_vm(self):
        self.row('azapi_update_resource')['start_utc']=(self.now-dt.timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        with self.assertRaisesRegex(ValueError,'share'):self.check()
    def test_timebox_and_session_deletion_required(self):
        self.row('azurerm_linux_virtual_machine')['same_session_delete']=False
        with self.assertRaises(ValueError):self.check()
    def test_delete_only_retains_rg_backend_and_checks_actual_disk_id(self):
        for r in self.plan['resource_changes']:
            if r['mode']=='managed':r['change']={'actions':['delete'],'before':r['change']['after'],'after':None}
        teardown.validate(self.plan,SUB)
        r=next(r for r in self.plan['resource_changes'] if r.get('type')=='azapi_update_resource');r['change']['before']['resource_id']=self.prefix+'Microsoft.Compute/disks/foreign'
        with self.assertRaises(ValueError):teardown.validate(self.plan,SUB)
if __name__=='__main__':unittest.main()
