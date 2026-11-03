"""Exact Task4 DNS lab scope. Independent of frozen Task3 state and guards."""
import datetime as dt
import copy
import hashlib
import ipaddress
import re

REGIONS={'a':'westus','b':'swedencentral'}
IPS={'a':'10.10.0.4','b':'10.20.0.4','home':'10.30.0.10'}
ZONE='dns.pathwatch.internal'
PREFIX='module.dns.'
TAGLESS={'azurerm_subnet','azurerm_network_security_rule','azurerm_subnet_network_security_group_association','azurerm_traffic_manager_azure_endpoint'}

def is_dns_plan(plan):
    return any(r.get('mode')=='managed' and (r.get('address','').startswith(PREFIX) or r.get('type','').startswith(('azurerm_private_dns_','azurerm_traffic_manager_'))) for r in plan.get('resource_changes',[]))
def label(sub):return 'pathwatch-'+hashlib.sha256(sub.encode()).hexdigest()[:10]+'-dns'
def address(typ,key=None):return PREFIX+typ+('.disk_tags' if typ=='azapi_update_resource' else '.this')+('' if key is None else '["'+key+'"]')
def resource_ids(sub):
    root=f'/subscriptions/{sub}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/'
    result={}
    for site in REGIONS:
        for typ,tail in {'azurerm_virtual_network':'virtualNetworks/vnet-pathwatch-dns-','azurerm_network_security_group':'networkSecurityGroups/nsg-pathwatch-dns-','azurerm_network_interface':'networkInterfaces/nic-pathwatch-dns-','azurerm_public_ip':'publicIPAddresses/pip-pathwatch-dns-'}.items():result[address(typ,site)]=root+tail+site
        subnet=root+'virtualNetworks/vnet-pathwatch-dns-'+site+'/subnets/snet-dns'
        result[address('azurerm_subnet',site)]=subnet
        result[address('azurerm_subnet_network_security_group_association',site)]=subnet
        for kind in ('ssh','wg','owner_http','managed_http','dns_tcp','dns_udp','deny'):
            result[address('azurerm_network_security_rule',site+'-'+kind)]=root+'networkSecurityGroups/nsg-pathwatch-dns-'+site+'/securityRules/'+kind
        for typ,tail in [('azurerm_linux_virtual_machine','virtualMachines/vm-'),('azapi_update_resource','disks/os-')]:
            result[address(typ,site)]=root.replace('Microsoft.Network/','Microsoft.Compute/')+tail+'pathwatch-dns-'+site
        result[address('azurerm_private_dns_zone_virtual_network_link',site)]=root+'privateDnsZones/'+ZONE+'/virtualNetworkLinks/link-pathwatch-dns-'+site
        result[address('azurerm_traffic_manager_azure_endpoint',site)]=root+'trafficManagerProfiles/'+label(sub)+'/azureEndpoints/'+site
    for key in IPS:result[address('azurerm_private_dns_a_record',key)]=root+'privateDnsZones/'+ZONE+'/A/'+key
    result[address('azurerm_private_dns_zone')]=root+'privateDnsZones/'+ZONE
    result[address('azurerm_traffic_manager_profile')]=root+'trafficManagerProfiles/'+label(sub)
    return result

def expected(sub,owner):
    """Reviewable exact values; references use deterministic owned ARM IDs, not unknowns."""
    ids=resource_ids(sub);out={}
    def add(typ,key=None,**fields):out[address(typ,key)]={'resource_group_name':'rg-pathwatch-lab',**fields}
    for site,region in REGIONS.items():
        stem='10.10.' if site=='a' else '10.20.'
        add('azurerm_virtual_network',site,name='vnet-pathwatch-dns-'+site,location=region,address_space=[stem+'0.0/16'],dns_servers=[],ddos_protection_plan=[],encryption=[])
        add('azurerm_subnet',site,name='snet-dns',virtual_network_name='vnet-pathwatch-dns-'+site,address_prefixes=[stem+'0.0/24'],default_outbound_access_enabled=False,delegation=[],service_endpoints=[],service_endpoint_policy_ids=[])
        add('azurerm_network_security_group',site,name='nsg-pathwatch-dns-'+site,location=region)
        add('azurerm_subnet_network_security_group_association',site,subnet_id=ids[address('azurerm_subnet',site)],network_security_group_id=ids[address('azurerm_network_security_group',site)])
        rules={'ssh':(100,'Tcp','22',owner),'wg':(110,'Udp','51824',owner),'owner_http':(120,'Tcp','80',owner),'managed_http':(130,'Tcp','80','AzureTrafficManager'),'dns_tcp':(140,'Tcp','53','10.30.0.10/32'),'dns_udp':(150,'Udp','53','10.30.0.10/32'),'deny':(4096,'*','*','*')}
        for kind,(priority,protocol,port,source) in rules.items():
            add('azurerm_network_security_rule',site+'-'+kind,name=kind,network_security_group_name='nsg-pathwatch-dns-'+site,priority=priority,direction='Inbound',access='Deny' if kind=='deny' else 'Allow',protocol=protocol,source_port_range='*',destination_port_range=port,source_address_prefix=source,destination_address_prefix='*' if kind=='deny' else IPS[site]+'/32',source_port_ranges=[],destination_port_ranges=[],source_address_prefixes=[],destination_address_prefixes=[],source_application_security_group_ids=[],destination_application_security_group_ids=[])
        add('azurerm_public_ip',site,name='pip-pathwatch-dns-'+site,location=region,allocation_method='Static',sku='Standard',sku_tier='Regional',ip_version='IPv4',domain_name_label=label(sub)+'-'+site,public_ip_prefix_id=None,ddos_protection_plan_id=None,ddos_protection_mode='VirtualNetworkInherited',zones=[])
        add('azurerm_network_interface',site,name='nic-pathwatch-dns-'+site,location=region,ip_forwarding_enabled=False,accelerated_networking_enabled=False,dns_servers=[],ip_configuration=[{'name':'primary','primary':True,'subnet_id':ids[address('azurerm_subnet',site)],'private_ip_address_allocation':'Static','private_ip_address':IPS[site],'public_ip_address_id':ids[address('azurerm_public_ip',site)]}])
        add('azurerm_linux_virtual_machine',site,name='vm-pathwatch-dns-'+site,location=region,size='Standard_B2pts_v2',admin_username='pathwatch',disable_password_authentication=True,allow_extension_operations=False,provision_vm_agent=True,patch_mode='ImageDefault',network_interface_ids=[ids[address('azurerm_network_interface',site)]],source_image_reference=[{'publisher':'Canonical','offer':'ubuntu-24_04-lts','sku':'server-arm64','version':'24.04.202610210'}],os_disk=[{'name':'os-pathwatch-dns-'+site,'storage_account_type':'Standard_LRS','disk_size_gb':30,'caching':'ReadWrite','diff_disk_settings':[],'disk_encryption_set_id':None,'secure_vm_disk_encryption_set_id':None,'security_encryption_type':None}],priority='Regular')
        out[address('azapi_update_resource',site)]={'type':'Microsoft.Compute/disks@2024-03-02','resource_id':ids[address('azapi_update_resource',site)]}
        add('azurerm_private_dns_zone_virtual_network_link',site,name='link-pathwatch-dns-'+site,private_dns_zone_name=ZONE,registration_enabled=False,virtual_network_id=ids[address('azurerm_virtual_network',site)],resolution_policy=None)
        out[address('azurerm_subnet_network_security_group_association',site)].pop('resource_group_name')
        out[address('azurerm_traffic_manager_azure_endpoint',site)]={'name':site,'profile_id':ids[address('azurerm_traffic_manager_profile')],'target_resource_id':ids[address('azurerm_public_ip',site)],'priority':1 if site=='a' else 2,'enabled':True,'always_serve_enabled':False,'geo_mappings':[],'custom_header':[],'subnet':[]}
    add('azurerm_private_dns_zone',name=ZONE)
    for key,ip in IPS.items():add('azurerm_private_dns_a_record',key,name=key,zone_name=ZONE,ttl=30,records=[ip])
    add('azurerm_traffic_manager_profile',name=label(sub),profile_status='Enabled',traffic_routing_method='Priority',traffic_view_enabled=False,dns_config=[{'relative_name':label(sub),'ttl':30}],monitor_config=[{'protocol':'HTTP','port':80,'path':'/healthz','interval_in_seconds':30,'timeout_in_seconds':5,'tolerated_number_of_failures':2,'custom_header':[],'expected_status_code_ranges':None}])
    return out

def parent(plan):
    name=PREFIX+'data.azurerm_resource_group.project'
    def walk(module):
        return module.get('resources',[])+[r for child in module.get('child_modules',[]) for r in walk(child)]
    sources=[[(r,r.get('change',{}).get('after')) for r in plan.get('resource_changes',[])],[(r,r.get('values')) for r in walk(plan.get('planned_values',{}).get('root_module',{}))],[(r,r.get('values')) for r in walk(plan.get('prior_state',{}).get('values',{}).get('root_module',{}))]]
    for source in sources:
        matches=[(r,v) for r,v in source if r.get('address')==name]
        if matches:
            if len(matches)!=1 or matches[0][0].get('mode')!='data' or not isinstance(matches[0][1],dict):raise ValueError('Invalid current parent RG; no stale fallback')
            return matches[0][1]
    raise ValueError('Read exact retained parent RG before DNS creation')

def provider_default_block(plan,typ,change,value,unknown,deleting):
    """Two pinned azurerm 4.63.0 defaults, never an optional-block wildcard."""
    key={'azurerm_linux_virtual_machine':'termination_notification','azurerm_private_dns_zone':'soa_record'}.get(typ)
    if key is None:return set()
    configs=plan.get('configuration',{}).get('root_module',{}).get('module_calls',{}).get('dns',{}).get('module',{}).get('resources',[])
    matches=[c for c in configs if c.get('address')==typ+'.this']
    if len(matches)!=1 or not isinstance(matches[0].get('expressions'),dict) or key in matches[0]['expressions']:raise ValueError('DNS provider default requires confirmed absent '+key+' configuration')
    if typ=='azurerm_linux_virtual_machine' and ('max_bid_price' in matches[0]['expressions'] or value.get('max_bid_price') not in (None,-1)):raise ValueError('Only unconfigured Regular VM default bid sentinel allowed')
    block=value.get(key);pending=unknown.get(key)
    if pending not in (None,False,[],{}):
        if not deleting and change['actions']==['create'] and pending is True and block is None:return {key}
        raise ValueError('Unexpected DNS provider default unknown shape: '+key)
    if block in (None,[]):return set()
    if key=='termination_notification':
        if block!=[{'enabled':False,'timeout':'PT5M'}]:raise ValueError('Unreviewed VM termination notification')
    else:
        # Azure's service-created SOA only. Serial is computed and may increase;
        # names, timings and metadata cannot introduce a custom record policy.
        expected={'expire_time':2419200,'minimum_ttl':10,'refresh_time':3600,'retry_time':300,'ttl':3600,'tags':{}}
        fields=set(expected)|{'email','host_name','fqdn','serial_number'}
        if not isinstance(block,list) or len(block)!=1 or not isinstance(block[0],dict):raise ValueError('Unexpected default SOA shape')
        record=block[0]
        if set(record)!=fields or any(record.get(k)!=v for k,v in expected.items()) or record['email'] not in ('azureprivatedns-host.microsoft.com','azureprivatedns-hostmaster.microsoft.com') or record['host_name'] not in ('azureprivatedns.net','azureprivatedns.net.') or record['fqdn'] not in (ZONE,ZONE+'.') or type(record['serial_number']) is not int or not 1<=record['serial_number']<=4294967295:raise ValueError('Unreviewed private DNS SOA default')
    return set()

def absent_expression(plan,typ,path):
    """Require a known configuration and absence at one exact optional path."""
    configs=plan.get('configuration',{}).get('root_module',{}).get('module_calls',{}).get('dns',{}).get('module',{}).get('resources',[])
    matches=[c for c in configs if c.get('address')==typ+'.this']
    if len(matches)!=1 or not isinstance(matches[0].get('expressions'),dict):return False
    value=matches[0]['expressions']
    for part in path.split('.'):
        if part=='0':
            if not isinstance(value,list) or len(value)!=1:return False
            value=value[0]
        else:
            if not isinstance(value,dict):return False
            if part not in value:return True
            value=value[part]
    return False

def empty_provider_defaults(plan,typ,change,value,unknown,deleting):
    """Normalize only observed, unconfigured empty defaults; preserve the input plan."""
    paths={
        'azurerm_linux_virtual_machine':{f'os_disk.0.{key}':'' for key in ('disk_encryption_set_id','secure_vm_disk_encryption_set_id','security_encryption_type')},
        'azurerm_traffic_manager_profile':{'monitor_config.0.expected_status_code_ranges':[]},
        'azurerm_network_interface':{'auxiliary_mode':'','auxiliary_sku':''},
        'azurerm_private_dns_zone_virtual_network_link':{'resolution_policy':''},
    }.get(typ,{})
    result=copy.deepcopy(value);allowed=set()
    for path,empty in paths.items():
        if not absent_expression(plan,typ,path):raise ValueError('DNS optional default must remain unconfigured: '+path)
        target=result
        for part in path.split('.')[:-1]:
            if part=='0':
                if not isinstance(target,list) or len(target)!=1:raise ValueError('DNS optional default nested shape mismatch')
                target=target[0]
            else:target=target.get(part)
        key=path.split('.')[-1]
        if not isinstance(target,dict):raise ValueError('DNS optional default nested shape mismatch')
        actual=target.get(key)
        if type(actual) is type(empty) and actual==empty:target[key]=None
    if typ=='azurerm_private_dns_zone_virtual_network_link':
        pending=unknown.get('resolution_policy')
        if pending is True and not deleting and change['actions']==['create'] and value.get('resolution_policy') is None:allowed.add('resolution_policy')
    return result,allowed

def validate(plan,sub,deleting=False):
    variables={k:v.get('value') for k,v in plan.get('variables',{}).items()}
    if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}',sub) or variables.get('subscription_id')!=sub:raise ValueError('DNS subscription mismatch')
    owner=variables.get('owner_cidr','');ip=ipaddress.ip_network(owner,strict=True)
    if ip.version!=4 or ip.prefixlen!=32 or not ip.network_address.is_global:raise ValueError('DNS owner requires actual global IPv4 /32')
    ttl=variables.get('ttl','')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',ttl):raise ValueError('Strict DNS UTC TTL required')
    dt.datetime.strptime(ttl,'%Y-%m-%dT%H:%M:%SZ')
    if not deleting:
        if variables.get('task3_cleanup_confirmed') is not True:raise ValueError('Task3 cleanup must precede DNS lab')
        if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',variables.get('campaign_id','')):raise ValueError('Durable campaign UUID required')
        p=parent(plan);tags=p.get('tags',{})
        if p.get('id','').lower()!=f'/subscriptions/{sub}/resourceGroups/rg-pathwatch-lab'.lower() or tags.get('project')!='pathwatch' or tags.get('owner')!='codex-pathwatch' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',tags.get('ttl','')) or ttl>tags['ttl']:raise ValueError('Retained parent ownership/TTL mismatch')
    wanted=expected(sub,owner);ids=resource_ids(sub);seen=set()
    def equal(actual,want):
        if isinstance(want,dict):return isinstance(actual,dict) and all(equal(actual.get(k),v) for k,v in want.items())
        if isinstance(want,list) and not want:return actual in (None,[])
        if isinstance(want,list):return isinstance(actual,list) and len(actual)==len(want) and all(equal(a,b) for a,b in zip(actual,want))
        return actual==want
    for r in plan.get('resource_changes',[]):
        if r.get('mode')!='managed':continue
        addr=r.get('address');typ=r.get('type');c=r['change'];v=c.get('before' if deleting else 'after')
        if addr not in wanted or addr in seen or addr.split('.')[2]!=typ or c['actions'] not in ([['delete'],['no-op']] if deleting else [['create'],['no-op']]) or not isinstance(v,dict):raise ValueError('DNS exact resources/create-only/delete-only required')
        seen.add(addr)
        unknown=r['change'].get('after_unknown',{}) if not deleting else {}
        allowed={
            'azapi_update_resource':{'id','ignore_casing','ignore_missing_property','name','output','parent_id'},
            'azurerm_linux_virtual_machine':{'id','computer_name','disk_controller_type','os_disk.0.id','os_managed_disk_id','patch_assessment_mode','private_ip_address','private_ip_addresses','public_ip_address','public_ip_addresses','virtual_machine_id','vm_agent_platform_updates_enabled'},
            'azurerm_network_interface':{'id','applied_dns_servers','internal_domain_name_suffix','ip_configuration.0.gateway_load_balancer_frontend_ip_configuration_id','mac_address','private_ip_address','private_ip_addresses','virtual_machine_id'},
            'azurerm_virtual_network':{'id','guid','subnet'},
            'azurerm_network_security_group':{'id','security_rule'},
            'azurerm_public_ip':{'id','fqdn','ip_address'},
            'azurerm_private_dns_zone':{'id','max_number_of_record_sets','max_number_of_virtual_network_links','max_number_of_virtual_network_links_with_registration','number_of_record_sets'},
            'azurerm_private_dns_a_record':{'id','fqdn'},
            'azurerm_traffic_manager_profile':{'id','fqdn'},
        }.get(typ,{'id'})
        def unknown_paths(value,path=''):
            if value is True:return {path}
            if isinstance(value,dict):return set().union(*(unknown_paths(v,path+'.'+k if path else k) for k,v in value.items())) if value else set()
            if isinstance(value,list):return set().union(*(unknown_paths(v,path+'.'+str(i)) for i,v in enumerate(value))) if value else set()
            return set()
        v,empty_allowed=empty_provider_defaults(plan,typ,c,v,unknown,deleting)
        allowed=allowed|empty_allowed|provider_default_block(plan,typ,c,v,unknown,deleting)
        if unknown_paths(unknown)-allowed:raise ValueError('Unknown DNS configurable input fails closed')
        if typ in ('azurerm_virtual_network','azurerm_network_security_group'):
            inline='subnet' if typ=='azurerm_virtual_network' else 'security_rule'
            configs=plan.get('configuration',{}).get('root_module',{}).get('module_calls',{}).get('dns',{}).get('module',{}).get('resources',[])
            config=next((c for c in configs if c.get('address')==typ+'.this'),None)
            if not deleting and (config is None or inline in config.get('expressions',{})):raise ValueError('Only separately managed DNS subnet/rules allowed')
            if v.get(inline):
                for child in v[inline]:
                    matches=[w for a,w in wanted.items() if a.startswith(PREFIX+('azurerm_subnet.' if inline=='subnet' else 'azurerm_network_security_rule.')) and w.get('name')==child.get('name') and (inline=='subnet' or w.get('network_security_group_name')==v.get('name'))]
                    if not matches:raise ValueError('Foreign inline network child')
        if not equal(v,wanted[addr]):raise ValueError('DNS resource differs from reviewed topology: '+addr)
        if deleting or v.get('id'):
            if str(v.get('id','')).lower()!=ids[addr].lower():raise ValueError('DNS exact ARM ID mismatch')
        if typ not in TAGLESS:
            tags=v.get('body',{}).get('tags',{}) if typ=='azapi_update_resource' else v.get('tags',{})
            if set(tags)!={'project','owner','ttl','est_usd_per_hour'} or tags.get('project')!='pathwatch' or tags.get('owner')!='codex-pathwatch' or tags.get('ttl')!=ttl:raise ValueError('DNS four tags/TTL required')
        if typ=='azurerm_network_interface' and (v.get('auxiliary_mode') not in (None,'None') or v.get('auxiliary_sku') not in (None,'None') or v.get('edge_zone') or v['ip_configuration'][0].get('gateway_load_balancer_frontend_ip_configuration_id')):raise ValueError('Unreviewed NIC add-on')
        if typ=='azapi_update_resource' and (set(v.get('body',{}))!={'tags'} or v.get('sensitive_body')):raise ValueError('Only exact implicit OS disk tag PATCH allowed')
        if typ=='azurerm_linux_virtual_machine':
            if any(v.get(k) for k in ('edge_zone','zone','proximity_placement_group_id','eviction_policy','encryption_at_host_enabled','secure_boot_enabled','vtpm_enabled')):raise ValueError('Unreviewed DNS VM placement/security/Spot option')
            if any(v['os_disk'][0].get(k) for k in ('write_accelerator_enabled','security_encryption_type','secure_vm_disk_encryption_set_id')):raise ValueError('Unreviewed OS disk option')
            forbidden=('admin_password','custom_data','user_data','source_image_id','plan','identity','additional_capabilities','boot_diagnostics','secret','gallery_application','dedicated_host_id','dedicated_host_group_id','capacity_reservation_group_id','availability_set_id','virtual_machine_scale_set_id','license_type')
            if any(v.get(k) for k in forbidden):raise ValueError('DNS VM add-on/secret forbidden')
            keys=v.get('admin_ssh_key',[])
            if len(keys)!=1 or keys[0].get('username')!='pathwatch' or keys[0].get('public_key')!=variables.get('admin_public_key'):raise ValueError('Exact reviewed SSH public key required')
    if not deleting and seen!=set(wanted):raise ValueError('DNS lab requires complete exact two-site resource set')
    return wanted
