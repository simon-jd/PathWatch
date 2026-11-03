"""Exact single-site probe/NVA Terraform root. No general VM update admission."""
import copy
import datetime as dt
import importlib.util
import ipaddress
from pathlib import Path
import re


def load(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
network=load('azure_network_scope');nva=load('azure_nva_scope');parent=load('azure_parent_renewal')
REGIONS={'azure-a':'westus','azure-b':'swedencentral'}
SKUS={'Standard_B2pts_v2':('server-arm64','Arm64'),'Standard_B2ats_v2':('server','x64')}
TYPES={'azurerm_public_ip','azurerm_network_interface','azurerm_linux_virtual_machine','azapi_update_resource'}


def matches(plan):
    return 'probe_site' in plan.get('variables',{}) or any(r.get('address','').startswith('module.probe.') for r in plan.get('resource_changes',[]))


def addresses(site):
    return {'azurerm_public_ip.probe':'azurerm_public_ip','azurerm_network_interface.probe':'azurerm_network_interface',
            f'module.probe.azurerm_linux_virtual_machine.probe["{site}"]':'azurerm_linux_virtual_machine',
            f'module.probe.azapi_update_resource.disk_tags["{site}"]':'azapi_update_resource'}


def arm_ids(subscription,site):
    prefix=f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab/providers/'
    types={'azurerm_public_ip':('Microsoft.Network/publicIPAddresses/','pip-'),'azurerm_network_interface':('Microsoft.Network/networkInterfaces/','nic-'),'azurerm_linux_virtual_machine':('Microsoft.Compute/virtualMachines/','vm-'),'azapi_update_resource':('Microsoft.Compute/disks/','os-')}
    return {address:prefix+types[typ][0]+types[typ][1]+'pathwatch-probe-'+site for address,typ in addresses(site).items()}


def validate(plan,subscription,deleting=False):
    variables={k:v.get('value') for k,v in plan.get('variables',{}).items()}
    site=variables.get('probe_site');location=variables.get('location');ttl=variables.get('ttl')
    if site not in REGIONS or location!=REGIONS[site] or variables.get('subscription_id')!=subscription:raise ValueError('Exact named probe site/subscription/region required')
    if plan.get('deferred_changes') or plan.get('resource_drift'):raise ValueError('Probe admission refuses deferred or drifted plans')
    resources=[r for r in plan.get('resource_changes',[]) if r.get('mode')=='managed']
    known=addresses(site);ids=arm_ids(subscription,site);stem='10.10' if site=='azure-a' else '10.20'
    network_plan=copy.deepcopy(plan)
    network_plan['resource_changes']=[r for r in resources if r.get('type') in network.NETWORK_TYPES]
    network_plan['variables']['name']={'value':'pathwatch-'+site}
    network.validate(network_plan,subscription,deleting)
    peers=variables.get('owner_peer_cidrs')
    if not isinstance(peers,list) or len(peers)!=1:raise ValueError('One explicit owner public /32 required')
    peer=ipaddress.ip_network(peers[0],strict=True)
    if peer.version!=4 or peer.prefixlen!=32 or not peer.network_address.is_global:raise ValueError('Owner peer must be public IPv4 /32')
    required={'module.network.azurerm_virtual_network.this','module.network.azurerm_network_security_group.this','module.network.azurerm_route_table.this','module.network.azurerm_network_security_rule.deny_ingress'}
    for key in ('workloads','nva','aks_nodes'):
        required|={f'module.network.azurerm_subnet.this["{key}"]',f'module.network.azurerm_subnet_network_security_group_association.this["{key}"]'}
        if key!='nva':required.add(f'module.network.azurerm_subnet_route_table_association.this["{key}"]')
    required|={f'module.network.azurerm_network_security_rule.{rule}["{peers[0]}"]' for rule in ('wireguard','wireguard_alternate','management')}
    required|={f'module.network.azurerm_route.home["{key}"]' for key in ('home','wireguard')}
    required|=set(known)
    seen=[r.get('address') for r in resources]
    if len(set(seen))!=len(seen) or (not deleting and set(seen)!=required) or (deleting and not set(seen)<=required):raise ValueError('Exact complete probe network/NIC/PIP/VM/disk resource set required')
    allowed_actions=([['delete'],['no-op']] if deleting else [['create'],['no-op']])
    for resource in resources:
        address=resource['address'];typ=resource.get('type');change=resource['change'];actions=change['actions']
        value=change.get('before' if deleting else 'after') or {}
        unknown={} if deleting else change.get('after_unknown',{})
        if actions not in allowed_actions:raise ValueError('Probe only accepts create/no-op or separate delete-only plans')
        if actions==['no-op'] and change.get('before')!=change.get('after'):raise ValueError('No-op current state mismatch')
        if actions==['no-op'] and (not value.get('id') or parent.unknown(change.get('after_unknown',{}))):raise ValueError('No-op requires fully known current ARM identity')
        if typ not in TYPES|network.NETWORK_TYPES:raise ValueError('Foreign probe resource type')
        tags=value.get('body',{}).get('tags') if typ=='azapi_update_resource' else value.get('tags')
        if typ in TYPES|network.PARENTS:
            if not isinstance(tags,dict) or set(tags)!={'project','owner','ttl','est_usd_per_hour'} or tags.get('project')!='pathwatch' or tags.get('owner')!='codex-pathwatch' or tags.get('ttl')!=ttl:raise ValueError('Exact four probe tags and common TTL required')
            if not deleting and parent.unknown(change.get('after_unknown',{}).get('tags',{})):raise ValueError('Unknown probe tags')
        if typ in network.NETWORK_TYPES:
            if typ=='azurerm_virtual_network' and parent.unknown(unknown.get('ddos_protection_plan')):raise ValueError('Unknown paid VNet DDoS configuration')
            if typ=='azurerm_network_security_rule':
                for field in ('source_application_security_group_ids','destination_application_security_group_ids','source_address_prefixes','source_port_ranges'):
                    if value.get(field) or parent.unknown(change.get('after_unknown',{}).get(field)):raise ValueError('Unreviewed plural NSG match or application group')
                if any(parent.unknown(unknown.get(field)) for field in ('destination_address_prefixes','destination_port_ranges')):raise ValueError('Unknown plural NSG destination match')
            if typ=='azurerm_route' and value.get('next_hop_in_ip_address')!=stem+'.1.4':raise ValueError('UDR must use exact single probe NVA')
            continue
        if known.get(address)!=typ:raise ValueError('Unknown probe VM/NIC/IP/disk address')
        expected=ids[address]
        if typ=='azapi_update_resource':
            if not deleting:
                # AzAPI sensitive_body is write-only: even a known after value
                # cannot establish its absence. Require the exact current HCL
                # configuration block, never missing/prior-state fallback.
                config=plan.get('configuration',{}).get('root_module',{}).get('module_calls',{}).get('probe',{}).get('module',{})
                patches=[r for r in config.get('resources',[]) if r.get('address')=='azapi_update_resource.disk_tags']
                if len(patches)!=1 or not isinstance(patches[0].get('expressions'),dict):raise ValueError('Exact disk_tags configuration expressions required')
                if any(k in patches[0]['expressions'] for k in ('sensitive_body','sensitive_body_version')):raise ValueError('Write-only disk mutation configuration is forbidden')
            if value.get('type')!='Microsoft.Compute/disks@2024-03-02' or set(value.get('body') or {})!={'tags'}:raise ValueError('Only implicit disk tags PATCH supported')
            network.require_id(value.get('resource_id'),expected)
            if any(value.get(k) or parent.unknown(unknown.get(k)) for k in ('sensitive_body','sensitive_body_version','update_headers','update_query_parameters','read_headers','read_query_parameters')):raise ValueError('No arbitrary disk API body/headers/query mutation')
            if not deleting and parent.unknown(change.get('after_unknown',{}).get('body')):raise ValueError('Disk tag body must be fully known')
        else:
            if value.get('name')!=expected.rsplit('/',1)[-1] or value.get('location')!=location or value.get('resource_group_name')!='rg-pathwatch-lab':raise ValueError('Probe exact name/group/region mismatch')
        if deleting or value.get('id'):network.require_id(value.get('id'),expected)
        if typ=='azurerm_public_ip':
            for field,wanted in dict(allocation_method='Static',sku='Standard',sku_tier='Regional',ip_version='IPv4').items():
                if value.get(field)!=wanted:raise ValueError('Only Standard regional static IPv4 permitted')
            if any(value.get(k) or parent.unknown(change.get('after_unknown',{}).get(k)) for k in ('public_ip_prefix_id','ddos_protection_plan_id','edge_zone','ip_tags','zones','domain_name_label','reverse_fqdn')) or parent.unknown(unknown.get('ddos_protection_mode')) or value.get('ddos_protection_mode') not in (None,'VirtualNetworkInherited'):raise ValueError('Public IP addon outside probe budget')
        elif typ=='azurerm_network_interface':
            if value.get('ip_forwarding_enabled') is not True or value.get('accelerated_networking_enabled') is not False or any(value.get(k) or parent.unknown(change.get('after_unknown',{}).get(k)) for k in ('dns_servers','auxiliary_mode','auxiliary_sku','edge_zone')):raise ValueError('Exact small NVA NIC configuration required')
            configs=value.get('ip_configuration',[])
            if len(configs)!=1:raise ValueError('Exactly one static probe NIC configuration required')
            config=configs[0]
            if any(config.get(k)!=v for k,v in dict(name='primary',primary=True,private_ip_address_allocation='Static',private_ip_address=stem+'.1.4').items()):raise ValueError('Static probe NIC address mismatch')
            network.require_id(config.get('subnet_id'),f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/virtualNetworks/vnet-pathwatch-{site}/subnets/snet-nva')
            network.require_id(config.get('public_ip_address_id'),ids['azurerm_public_ip.probe'])
            exprs=plan.get('configuration',{}).get('root_module',{}).get('resources',[])
            expr=next((r.get('expressions',{}).get('ip_configuration',[]) for r in exprs if r.get('address')==address),None)
            if config.get('gateway_load_balancer_frontend_ip_configuration_id') or (not deleting and (not isinstance(expr,list) or len(expr)!=1 or 'gateway_load_balancer_frontend_ip_configuration_id' in expr[0])):raise ValueError('No configured gateway load balancer NIC attachment')
        elif typ=='azurerm_linux_virtual_machine':
            sku=value.get('size');image=value.get('source_image_reference',[]);disk=value.get('os_disk',[])
            if sku not in SKUS or sku!=variables.get('vm_size') or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',variables.get('image_version','')):raise ValueError('Explicit small CPU SKU and pinned image required')
            expected_image=dict(publisher='Canonical',offer='ubuntu-24_04-lts',sku=SKUS[sku][0],version=variables['image_version'])
            if len(image)!=1 or any(image[0].get(k)!=v for k,v in expected_image.items()):raise ValueError('Pinned Ubuntu image architecture mismatch')
            if value.get('admin_username')!='pathwatch' or value.get('disable_password_authentication') is not True or value.get('allow_extension_operations') is not False or value.get('provision_vm_agent') is not True or parent.unknown(unknown.get('priority')) or value.get('priority') not in (None,'Regular'):raise ValueError('Probe VM auth/extension/priority policy mismatch')
            forbidden=('admin_password','source_image_id','plan','identity','custom_data','user_data','additional_capabilities','data_disk','boot_diagnostics','secret','gallery_application','dedicated_host_id','dedicated_host_group_id','capacity_reservation_group_id','license_type','availability_set_id','proximity_placement_group_id','virtual_machine_scale_set_id','edge_zone','zone')
            if any(value.get(k) or parent.unknown(change.get('after_unknown',{}).get(k)) for k in forbidden):raise ValueError('No VM addons/extensions/secrets/spot/foreign reservations')
            keys=value.get('admin_ssh_key',[])
            if len(keys)!=1 or keys[0].get('username')!='pathwatch' or not re.fullmatch(r'ssh-(?:ed25519|rsa) [A-Za-z0-9+/=]+(?: [^\n]*)?',keys[0].get('public_key','')):raise ValueError('One explicit guest SSH key required')
            # In this nested block only the disk ARM id is provider-computed.
            # Never allow an unknown block, encryption reference or other config
            # merely because the corresponding current after value is empty.
            disk_unknown=unknown.get('os_disk',[])
            if parent.unknown(disk_unknown) and (not isinstance(disk_unknown,list) or len(disk_unknown)!=1 or not isinstance(disk_unknown[0],dict) or any(parent.unknown(v) for k,v in disk_unknown[0].items() if k!='id')):raise ValueError('Unknown OS disk configuration; only computed id is permitted')
            if len(disk)!=1 or any(disk[0].get(k)!=v for k,v in dict(name='os-pathwatch-probe-'+site,storage_account_type='Standard_LRS',disk_size_gb=30,caching='ReadWrite').items()) or any(disk[0].get(k) for k in ('diff_disk_settings','disk_encryption_set_id','secure_vm_disk_encryption_set_id','security_encryption_type','write_accelerator_enabled')):raise ValueError('Exact named 30GB Standard HDD disk required')
            if value.get('network_interface_ids')!=[ids['azurerm_network_interface.probe']]:raise ValueError('VM must attach only the single exact probe NIC')
    if not deleting:
        for resource in plan.get('resource_changes',[]):
            if resource.get('mode')=='data' and (resource.get('address')!='module.network.data.azurerm_resource_group.project' or parent.unknown(resource.get('change',{}).get('after_unknown'))):raise ValueError('Unknown/unreviewed current parent data')
        value=nva.parent_resource_group(plan);tags=value.get('tags',{})
        for timestamp in (ttl,tags.get('ttl')):
            if not isinstance(timestamp,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',timestamp):raise ValueError('Strict current parent and child UTC TTL required')
            dt.datetime.strptime(timestamp,'%Y-%m-%dT%H:%M:%SZ')
        network.require_id(value.get('id'),f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab')
        if set(tags)!={'project','owner','ttl','est_usd_per_hour'} or tags.get('project')!='pathwatch' or tags.get('owner')!='codex-pathwatch' or tags.get('ttl')!=variables.get('parent_ttl') or not isinstance(ttl,str) or ttl>tags['ttl']:raise ValueError('Current owned parent must cover exact probe lifetime')
    return site
