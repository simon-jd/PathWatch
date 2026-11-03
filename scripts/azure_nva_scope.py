"""Task3 exact Azure A NVA state. No generic VM, arbitrary PATCH or update admission."""
import copy
import datetime as dt
import ipaddress
import hashlib
import re

TYPES={'azurerm_linux_virtual_machine','azurerm_network_interface','azurerm_public_ip','azapi_update_resource'}
TAGGABLE={'azurerm_linux_virtual_machine','azurerm_network_interface','azurerm_public_ip'}
IMAGE={'publisher':'Canonical','offer':'ubuntu-24_04-lts','sku':'server-arm64','version':'24.04.202610210'}

def is_nva_plan(plan):
    return any(r.get('mode')=='managed' and r.get('type') in TYPES for r in plan.get('resource_changes',[]))

def parent_resource_group(plan):
    """Terraform may retain an already-read data source only in prior_state.

    Current/deferred representations are authoritative. Never hide an invalid
    current value by falling back to a valid older snapshot.
    """
    address='module.network.data.azurerm_resource_group.project'
    def nested(module):
        if not isinstance(module,dict):raise ValueError('Malformed parent RG state module')
        resources=module.get('resources',[]);children=module.get('child_modules',[])
        if not isinstance(resources,list) or not isinstance(children,list):raise ValueError('Malformed parent RG state collection')
        found=[item for item in resources if isinstance(item,dict) and item.get('address')==address]
        for child in children:found.extend(nested(child))
        return found
    current=[item for item in plan.get('resource_changes',[]) if item.get('address')==address]
    def representations():
        yield current,True
        yield nested(plan.get('planned_values',{}).get('root_module',{})),False
        yield nested(plan.get('prior_state',{}).get('values',{}).get('root_module',{})),False
    for matches,changed in representations():
        if not matches:continue
        if len(matches)!=1:raise ValueError('Ambiguous parent RG data source')
        item=matches[0]
        if item.get('mode')!='data' or item.get('type')!='azurerm_resource_group':raise ValueError('Parent must be the exact RG data source')
        value=item.get('change',{}).get('after') if changed else item.get('values')
        if not isinstance(value,dict) or not value:raise ValueError('Current parent RG data is missing or deferred; do not use older state')
        return value
    raise ValueError('Read existing parent RG ownership and TTL before NVA creation')

def validate(plan,subscription,network_scope,deleting=False):
    if plan.get('variables',{}).get('subscription_id',{}).get('value')!=subscription or not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}',subscription):raise ValueError('NVA subscription mismatch')
    if plan.get('variables',{}).get('location',{}).get('value')!='westus':raise ValueError('NVA only reviewed in westus')
    resources=[r for r in plan.get('resource_changes',[]) if r.get('mode')=='managed']
    if any(r['type'] not in TYPES|network_scope.NETWORK_TYPES for r in resources):raise ValueError('Hybrid state cannot own shared RG/backend or foreign types')
    networks=copy.deepcopy(plan);networks['resource_changes']=[r for r in resources if r['type'] in network_scope.NETWORK_TYPES]
    if networks['resource_changes']:
        if network_scope.validate(networks,subscription,deleting)!= 'pathwatch-azure-a':raise ValueError('Hybrid network must be Azure A')
    elif not deleting:raise ValueError('NVA creation requires owned network in same state')
    prefix=f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab/providers/'
    config=plan.get('configuration',{}).get('root_module',{}).get('module_calls',{}).get('appliance',{}).get('module',{})
    configs={r.get('address'):r for r in config.get('resources',[])}
    def exact(value,expected):
        if not isinstance(value,str) or value.lower()!=expected.lower():raise ValueError('NVA escaped exact owned ARM ID')
    def reference(resource,value,field,expected,ref):
        candidate=value.get(field)
        if candidate is not None:
            if isinstance(expected,list):
                if len(candidate)!=len(expected):raise ValueError('Unexpected NIC count')
                for actual,wanted in zip(candidate,expected):exact(actual,wanted)
            else:exact(candidate,expected)
            return
        unknown=resource['change'].get('after_unknown',{}).get(field)
        refs=configs.get(resource['type']+'.this',{}).get('expressions',{}).get(field,{}).get('references',[])
        if deleting or not unknown or ref not in refs or not set(refs)<={ref,ref+'.id','each.key'}:raise ValueError('Unknown NVA association must reference own indexed resource')
    seen=set();nodes=set()
    for r in resources:
        typ=r['type']
        if typ not in TYPES:continue
        address=r['address'];actions=r['change']['actions'];v=r['change'].get('before' if deleting else 'after') or {}
        match=re.fullmatch(r'module\.appliance\.'+re.escape(typ)+r'\.'+('disk_tags' if typ=='azapi_update_resource' else 'this')+r'\["(nva|client)"\]',address)
        if not match or address in seen or actions not in ([['delete'],['no-op']] if deleting else [['create'],['no-op']]):raise ValueError('Only known hybrid resources and creates/delete-only accepted')
        seen.add(address);node=match[1];nodes.add(node)
        name={'azurerm_linux_virtual_machine':'vm-','azurerm_network_interface':'nic-','azurerm_public_ip':'pip-','azapi_update_resource':'os-'}[typ]+'pathwatch-hybrid-'+node
        provider={'azurerm_linux_virtual_machine':'Microsoft.Compute/virtualMachines/','azurerm_network_interface':'Microsoft.Network/networkInterfaces/','azurerm_public_ip':'Microsoft.Network/publicIPAddresses/','azapi_update_resource':'Microsoft.Compute/disks/'}[typ]
        expected=prefix+provider+name
        if typ=='azapi_update_resource':
            if v.get('type')!='Microsoft.Compute/disks@2024-03-02' or set(v.get('body') or {})!={'tags'}:raise ValueError('Only exact disk tags PATCH is admitted')
            exact(v.get('resource_id'),expected)
            tags=v['body']['tags']
        else:
            if v.get('name')!=name or v.get('location')!='westus' or v.get('resource_group_name')!='rg-pathwatch-lab':raise ValueError('NVA name/region/group mismatch')
            tags=v.get('tags') or {}
        if tags.get('project')!='pathwatch' or tags.get('owner')!='codex-pathwatch' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',tags.get('ttl','')):raise ValueError('NVA four ownership tags required')
        if deleting or v.get('id'):exact(v.get('id'),expected)
        if typ=='azurerm_public_ip':
            label='pathwatch-'+hashlib.sha256(subscription.encode()).hexdigest()[:10]+'-'+node
            if v.get('domain_name_label')!=label:raise ValueError('PIP requires stable project+subscription scoped free Azure DNS label')
            if any(v.get(k)!=x for k,x in {'allocation_method':'Static','sku':'Standard','sku_tier':'Regional','ip_version':'IPv4'}.items()) or v.get('public_ip_prefix_id') or v.get('ddos_protection_mode') not in (None,'VirtualNetworkInherited'):raise ValueError('Only static Regional Standard IPv4 without paid add-ons')
        elif typ=='azurerm_network_interface':
            if v.get('ip_forwarding_enabled')!=(node=='nva') or v.get('accelerated_networking_enabled') is not False or v.get('dns_servers'):raise ValueError('NVA forwarding/NIC settings changed')
            ips=v.get('ip_configuration') or []
            if len(ips)!=1:raise ValueError('Exactly one static NIC configuration required')
            ip=ips[0]
            if ip.get('name')!='primary' or ip.get('primary') is not True or ip.get('private_ip_address_allocation')!='Static' or ip.get('private_ip_address')!=('10.10.1.4' if node=='nva' else '10.10.0.4'):raise ValueError('Static NVA/client address mismatch')
            exact(ip.get('subnet_id'),prefix+'Microsoft.Network/virtualNetworks/vnet-pathwatch-azure-a/subnets/snet-'+('nva' if node=='nva' else 'workloads'))
            # Nested expression names are preserved in actual configuration JSON.
            if ip.get('public_ip_address_id') is None:
                refs=configs.get(typ+'.this',{}).get('expressions',{}).get('ip_configuration',[{}])
                if isinstance(refs,list):refs=refs[0].get('public_ip_address_id',{}).get('references',[])
                else:refs=[]
                if deleting or 'azurerm_public_ip.this' not in refs or not set(refs)<={'azurerm_public_ip.this','each.key'}:raise ValueError('Unknown public IP must reference own indexed PIP')
            else:exact(ip['public_ip_address_id'],prefix+'Microsoft.Network/publicIPAddresses/pip-pathwatch-hybrid-'+node)
        elif typ=='azurerm_linux_virtual_machine':
            if v.get('size')!='Standard_B2pts_v2' or v.get('admin_username')!='pathwatch' or v.get('disable_password_authentication') is not True or v.get('allow_extension_operations') is not False or v.get('source_image_id') or v.get('plan') or v.get('identity') or v.get('custom_data') or v.get('user_data') or v.get('additional_capabilities') or v.get('data_disk') or v.get('boot_diagnostics') or v.get('secret') or v.get('gallery_application') or v.get('dedicated_host_id') or v.get('dedicated_host_group_id') or v.get('capacity_reservation_group_id') or v.get('priority','Regular')!='Regular':raise ValueError('Only reviewed ARM64 Linux VM without extensions/secrets/addons')
            image=v.get('source_image_reference') or []
            if len(image)!=1 or any(image[0].get(k)!=x for k,x in IMAGE.items()):raise ValueError('Pinned ARM64 V2 Ubuntu image required')
            disk=v.get('os_disk') or []
            if len(disk)!=1 or disk[0].get('name')!='os-pathwatch-hybrid-'+node or disk[0].get('storage_account_type')!='Standard_LRS' or disk[0].get('disk_size_gb') not in (30,31,32) or disk[0].get('diff_disk_settings') or disk[0].get('disk_encryption_set_id'):raise ValueError('Only named 30–32GB Standard HDD OS disk')
            reference(r,v,'network_interface_ids',[prefix+'Microsoft.Network/networkInterfaces/nic-pathwatch-hybrid-'+node],'azurerm_network_interface.this')
    if not deleting:
        if 'nva' not in nodes:raise ValueError('NVA absent')
        for node in nodes:
            for typ in TYPES:
                address=f'module.appliance.{typ}.{"disk_tags" if typ=="azapi_update_resource" else "this"}["{node}"]'
                if address not in seen:raise ValueError('Every VM requires owned NIC/PIP and explicit OS disk tag manager')
        owner=plan.get('variables',{}).get('owner_peer_cidrs',{}).get('value')
        if not isinstance(owner,list) or len(owner)!=1:raise ValueError('Hybrid requires one actual owned public /32 peer')
        peer=owner[0]
        public=ipaddress.ip_network(peer,strict=True)
        if public.version!=4 or public.prefixlen!=32 or not public.network_address.is_global:raise ValueError('Hybrid public peer must be global IPv4 /32')
        by_address={r['address']:r['change'].get('after') or {} for r in resources}
        for resource,port in [('wireguard','51820'),('wireguard_alternate','51821')]:
            value=by_address.get(f'module.network.azurerm_network_security_rule.{resource}["{peer}"]',{})
            if value.get('destination_port_range')!=port or value.get('source_address_prefix')!=peer:raise ValueError('Both independently impairable WG ports require exact same owner /32')
        for key in ('icmp','iperf'):
            if f'module.network.azurerm_network_security_rule.hybrid_forwarding["{key}"]' not in by_address:raise ValueError('NVA forwarding requires actual home-destination transit NSG rules')
            if f'module.network.azurerm_network_security_rule.hybrid_outbound["{key}"]' not in by_address:raise ValueError('NVA forwarding requires actual home-source outbound NSG rules')
        for key,cidr in [('home','10.30.0.0/16'),('wireguard','10.255.0.0/24')]:
            value=by_address.get(f'module.network.azurerm_route.home["{key}"]',{})
            if value.get('address_prefix')!=cidr or value.get('next_hop_in_ip_address')!='10.10.1.4':raise ValueError('Workload UDR must target this exact NVA NIC')
        parent=parent_resource_group(plan)
        parent_tags=parent.get('tags')
        if not isinstance(parent_tags,dict) or parent_tags.get('project')!='pathwatch' or parent_tags.get('owner')!='codex-pathwatch':raise ValueError('Read existing parent RG ownership and TTL before NVA creation')
        exact(parent.get('id'),f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab')
        ttl=parent_tags.get('ttl','')
        if not isinstance(ttl,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',ttl):raise ValueError('Parent RG requires strict UTC expiry')
        dt.datetime.strptime(ttl,'%Y-%m-%dT%H:%M:%SZ')
        for r in resources:
            v=r['change'].get('after') or {};tags=v.get('body',{}).get('tags',{}) if r['type']=='azapi_update_resource' else v.get('tags',{})
            if tags and tags.get('ttl','')>ttl:raise ValueError('Child expiry exceeds retained bootstrap parent TTL')
