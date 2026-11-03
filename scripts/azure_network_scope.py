"""Exact network-only admission/deletion scope. Never owns shared RG or backend."""
import ipaddress
import re

NETWORK_TYPES = {
    'azurerm_virtual_network', 'azurerm_subnet', 'azurerm_network_security_group',
    'azurerm_network_security_rule', 'azurerm_route_table', 'azurerm_route',
    'azurerm_subnet_network_security_group_association', 'azurerm_subnet_route_table_association',
}
PARENTS = {'azurerm_virtual_network', 'azurerm_network_security_group', 'azurerm_route_table'}
LOCATIONS = {'westus', 'francecentral', 'norwayeast', 'denmarkeast', 'swedencentral'}
PRICE_SOURCE = 'docs/azure-prices/2026-10-23-network-components.json'
NAME = r'pathwatch-(?:azure-[ab]|it-[0-9a-f]{12})'
SUBNETS = {'workloads': ('0.0/24', 0), 'nva': ('1.0/27', 1), 'aks_nodes': ('16.0/20', 16)}


def is_network_plan(plan):
    return any(r.get('mode') == 'managed' and r.get('type') in NETWORK_TYPES
               for r in plan.get('resource_changes', [])) or bool(re.fullmatch(NAME, plan.get('variables', {}).get('name', {}).get('value', '')))


def require_id(value, expected):
    if not isinstance(value, str) or value.lower() != expected.lower():
        raise ValueError('Network resource ID escaped exact owned network parent')


def child_key(address, typ, resource='this'):
    match = re.fullmatch(r'module\.network\.'+re.escape(typ)+r'\.'+resource+r'\["([^"\n]+)"\]', address)
    if not match:
        raise ValueError('Unexpected network Terraform resource address')
    return match[1]


def validate(plan, subscription, deleting=False):
    """Validate generated binary plan scope, including unknown association references.

    Create IDs are not yet allocated. Only a recognized configuration expression
    pointing to this module's own resources may stand in for an unknown child ID.
    Destroy requires actual before-state IDs and parent ownership instead.
    """
    if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', subscription):
        raise ValueError('Explicit subscription UUID required')
    if plan.get('variables', {}).get('subscription_id', {}).get('value') != subscription:
        raise ValueError('Network plan subscription mismatch')
    location = plan.get('variables', {}).get('location', {}).get('value')
    if location not in LOCATIONS:
        raise ValueError('Network region is outside allowed subscription policy')
    resources = [r for r in plan.get('resource_changes', []) if r.get('mode') == 'managed']
    if any(r.get('type') not in NETWORK_TYPES for r in resources):
        raise ValueError('Network state must not include shared RG/backend/identity or paid resources')
    values = [(r, r['change'].get('before' if deleting else 'after') or {}) for r in resources]
    names = set()
    for resource, value in values:
        typ = resource['type']
        if typ in PARENTS:
            prefix = {'azurerm_virtual_network': 'vnet-', 'azurerm_network_security_group': 'nsg-', 'azurerm_route_table': 'rt-'}[typ]
            name = value.get('name', '')
            if not name.startswith(prefix) or not re.fullmatch(NAME, name[len(prefix):]):
                raise ValueError('Network parent name is not an authorized regional/test prefix')
            names.add(name[len(prefix):])
    declared_name = plan.get('variables', {}).get('name', {}).get('value')
    if declared_name is not None:
        if not re.fullmatch(NAME, declared_name):
            raise ValueError('Invalid fixture name')
        names.add(declared_name)
    if len(names) != 1:
        raise ValueError('Cannot prove exactly one owned regional network prefix')
    name = names.pop()
    if not deleting and not any(r['type'] == 'azurerm_virtual_network' for r in resources):
        raise ValueError('Network admission requires the owned VNet in the plan')
    regional_cidr = {'pathwatch-azure-a': '10.10.0.0/16', 'pathwatch-azure-b': '10.20.0.0/16'}.get(name)
    expected_cidr = regional_cidr or plan.get('variables', {}).get('address_space', {}).get('value')
    vnet_value = next((v for r, v in values if r['type'] == 'azurerm_virtual_network'), None)
    if vnet_value is not None:
        spaces = vnet_value.get('address_space')
        if not isinstance(spaces, list) or len(spaces) != 1:
            raise ValueError('VNet address_space must contain exactly one assigned Azure /16')
        cidr = spaces[0]
    else:
        # Admission already requires a VNet. Only a genuinely missing VNet in a
        # partial teardown may use the root/fixture assignment to scope children.
        cidr = expected_cidr
    if not isinstance(cidr, str) or cidr not in {'10.10.0.0/16', '10.20.0.0/16'}:
        raise ValueError('Network must retain an assigned Azure /16')
    if cidr != expected_cidr:
        raise ValueError('Regional network CIDR changed' if regional_cidr else 'Fixture network CIDR changed from expected assignment')
    prefix = f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/'
    vnet = prefix+'virtualNetworks/vnet-'+name
    nsg = prefix+'networkSecurityGroups/nsg-'+name
    rt = prefix+'routeTables/rt-'+name
    stem = cidr.removesuffix('0.0/16')
    subnets = {key: vnet+'/subnets/snet-'+key for key in SUBNETS}
    config = plan.get('configuration', {}).get('root_module', {}).get('module_calls', {}).get('network', {}).get('module', {})
    configs = {r.get('address'): r for r in config.get('resources', [])}
    seen = set()

    def reference(resource, value, field, expected, own_reference):
        if value.get(field) is not None:
            require_id(value[field], expected)
            return
        refs = configs.get(resource['type']+'.this', {}).get('expressions', {}).get(field, {}).get('references', [])
        allowed = {own_reference, own_reference.removesuffix('.id'), 'each.key'}
        expected_ref = own_reference.removesuffix('.id') if field == 'subnet_id' else own_reference
        if deleting or not resource['change'].get('after_unknown', {}).get(field) or expected_ref not in refs or not set(refs) <= allowed:
            raise ValueError('Unknown association must reference only this module owned resource')

    if not deleting and any(r['type'] == 'azurerm_virtual_network' and r['change']['actions'] == ['create'] for r in resources):
        required = {'module.network.azurerm_virtual_network.this', 'module.network.azurerm_network_security_group.this', 'module.network.azurerm_route_table.this', 'module.network.azurerm_network_security_rule.deny_ingress'}
        for key in SUBNETS:
            required.add(f'module.network.azurerm_subnet.this["{key}"]')
            required.add(f'module.network.azurerm_subnet_network_security_group_association.this["{key}"]')
            if key != 'nva': required.add(f'module.network.azurerm_subnet_route_table_association.this["{key}"]')
        if not required <= {r['address'] for r in resources}:
            raise ValueError('New network must include default deny and all required subnet associations')
    for resource, value in values:
        typ = resource['type']; address = resource['address']; actions = resource['change']['actions']
        if address in seen or actions not in ([['delete'], ['no-op']] if deleting else [['create'], ['no-op']]):
            raise ValueError('Network guard refuses duplicate addresses, updates and replacements')
        seen.add(address)
        if value.get('resource_group_name', 'rg-pathwatch-lab') != 'rg-pathwatch-lab':
            raise ValueError('Network escaped project group')
        if typ in PARENTS:
            if address != 'module.network.'+typ+'.this' or value.get('location') != location:
                raise ValueError('Network parent address/location mismatch')
            tags = value.get('tags') or {}
            if tags.get('project') != 'pathwatch' or tags.get('owner') != 'codex-pathwatch' or tags.get('est_usd_per_hour') != '0' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', tags.get('ttl', '')):
                raise ValueError('Network parent ownership/TTL/zero-estimate mismatch')
            inline = {'azurerm_virtual_network': 'subnet', 'azurerm_network_security_group': 'security_rule', 'azurerm_route_table': 'route'}[typ]
            if actions == ['create'] and value.get(inline):
                raise ValueError('Inline network children bypass exact scope; use recognized standalone resources')
            expected = {'azurerm_virtual_network': vnet, 'azurerm_network_security_group': nsg, 'azurerm_route_table': rt}[typ]
            if deleting or value.get('id'):
                require_id(value.get('id'), expected)
            if typ == 'azurerm_virtual_network' and value.get('ddos_protection_plan'):
                raise ValueError('Paid DDoS plan is outside network-only scope')
        elif typ == 'azurerm_subnet':
            key = child_key(address, typ)
            if key not in SUBNETS or value.get('name') != 'snet-'+key or value.get('virtual_network_name') != 'vnet-'+name or value.get('address_prefixes') != [stem+SUBNETS[key][0]] or value.get('default_outbound_access_enabled') is not False:
                raise ValueError('Subnet name/CIDR/private-outbound mismatch')
            if deleting or value.get('id'):
                require_id(value.get('id'), subnets[key])
        elif typ in {'azurerm_subnet_network_security_group_association', 'azurerm_subnet_route_table_association'}:
            key = child_key(address, typ)
            if key not in subnets or (typ.endswith('route_table_association') and key == 'nva'):
                raise ValueError('Unexpected subnet association or NVA self-routing')
            reference(resource, value, 'subnet_id', subnets[key], 'azurerm_subnet.this.id')
            field, expected, ref = ('network_security_group_id', nsg, 'azurerm_network_security_group.this.id') if typ.endswith('security_group_association') else ('route_table_id', rt, 'azurerm_route_table.this.id')
            reference(resource, value, field, expected, ref)
            if deleting or value.get('id'):
                require_id(value.get('id'), subnets[key])
        elif typ == 'azurerm_network_security_rule':
            outbound = '.hybrid_outbound[' in address
            if value.get('network_security_group_name') != 'nsg-'+name or value.get('direction') != ('Outbound' if outbound else 'Inbound') or value.get('source_port_range') != '*':
                raise ValueError('NSG rule parent/direction mismatch')
            rule_name = value.get('name', '')
            if address == 'module.network.azurerm_network_security_rule.deny_ingress':
                if any(value.get(k) != expected for k, expected in {'name': 'deny-all-inbound', 'access': 'Deny', 'priority': 4096, 'protocol': '*', 'source_address_prefix': '*', 'destination_address_prefix': '*', 'destination_port_range': '*'}.items()):
                    raise ValueError('Explicit default deny ingress was weakened')
            else:
                if value.get('access') != 'Allow' or not 100 <= value.get('priority', 0) < 4096:
                    raise ValueError('Unexpected NSG allow priority/access')
                if '.private[' in address:
                    key = child_key(address, typ, 'private')
                    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', key) or rule_name != 'allow-private-'+key:
                        raise ValueError('Unexpected private rule name')
                    source = ipaddress.ip_network(value.get('source_address_prefix', ''), strict=True)
                    if not any(source.subnet_of(ipaddress.ip_network(allowed)) for allowed in ('10.10.0.0/16', '10.20.0.0/16', '10.30.0.0/16', '10.255.0.0/24')) or value.get('destination_address_prefix') not in {stem+suffix for suffix, _ in SUBNETS.values()}:
                        raise ValueError('Private allow escaped owned source/destination scope')
                    if value.get('protocol') == 'Icmp':
                        if value.get('destination_port_range') != '*' or value.get('destination_port_ranges'):
                            raise ValueError('Invalid ICMP rule')
                    elif value.get('protocol') in {'Tcp', 'Udp'}:
                        ports = value.get('destination_port_ranges') or []
                        if value.get('destination_port_range') or not ports or any(not str(p).isdigit() or not 1 <= int(p) <= 65535 or int(p) in {22, 3389} for p in ports):
                            raise ValueError('Administrative/wildcard ports forbidden')
                    else:
                        raise ValueError('Private rule protocol must be explicit')
                elif '.wireguard[' in address or '.wireguard_alternate[' in address:
                    alternate = '.wireguard_alternate[' in address
                    peer = child_key(address, typ, 'wireguard_alternate' if alternate else 'wireguard')
                    network = ipaddress.ip_network(peer, strict=True)
                    if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global or value.get('source_address_prefix') != peer or value.get('destination_address_prefix') != stem+'1.0/27' or value.get('protocol') != 'Udp' or value.get('destination_port_range') != ('51821' if alternate else '51820') or value.get('destination_port_ranges'):
                        raise ValueError('WireGuard must allow an owned public /32 only to NVA UDP51820')
                elif outbound:
                    key = child_key(address, typ, 'hybrid_outbound')
                    expected = {'icmp': ('Icmp', '*', 2600), 'iperf': ('Tcp', '5201', 2601)}.get(key)
                    if expected is None or rule_name != 'allow-hybrid-outbound-'+key or value.get('source_address_prefix') != '10.30.0.0/16' or value.get('destination_address_prefix') != stem+'0.0/24' or value.get('protocol') != expected[0] or value.get('destination_port_range') != expected[1] or value.get('priority') != expected[2]:
                        raise ValueError('Hybrid outbound only home-to-workload ICMP/TCP5201')
                    if any(value.get(field) for field in ('source_address_prefixes', 'destination_address_prefixes', 'source_port_ranges', 'destination_port_ranges', 'source_application_security_group_ids', 'destination_application_security_group_ids')):
                        raise ValueError('Hybrid outbound cannot add plural prefixes/ports or application groups')
                elif '.hybrid_forwarding[' in address:
                    key = child_key(address, typ, 'hybrid_forwarding')
                    expected = {'icmp':('Icmp','*'),'iperf':('Tcp','5201')}.get(key)
                    if expected is None or rule_name != 'allow-hybrid-forward-'+key or value.get('source_address_prefix')!=stem+'0.0/24' or value.get('destination_address_prefix')!='10.30.0.0/16' or value.get('protocol')!=expected[0] or value.get('destination_port_range')!=expected[1] or value.get('destination_port_ranges'):
                        raise ValueError('Hybrid forwarding only workload-to-home ICMP/TCP5201')
                elif '.management[' in address:
                    peer = child_key(address, typ, 'management')
                    network = ipaddress.ip_network(peer, strict=True)
                    if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global or value.get('source_address_prefix') != peer or set(value.get('destination_address_prefixes') or []) != {stem+'1.0/27',stem+'0.0/24'} or value.get('destination_address_prefix') or value.get('protocol') != 'Tcp' or value.get('destination_port_range') != '22' or value.get('destination_port_ranges'):
                        raise ValueError('Management requires actual owned public /32 and SSH only to NVA/workload subnets')
                else:
                    raise ValueError('Unrecognized network rule address')
            if deleting or value.get('id'):
                require_id(value.get('id'), nsg+'/securityRules/'+rule_name)
        elif typ == 'azurerm_route':
            key = child_key(address, typ, 'home')
            expected_range = {'home': '10.30.0.0/16', 'wireguard': '10.255.0.0/24'}.get(key)
            ip = ipaddress.ip_address(value.get('next_hop_in_ip_address', ''))
            if not expected_range or value.get('name') != 'to-'+key or value.get('route_table_name') != 'rt-'+name or value.get('address_prefix') != expected_range or value.get('next_hop_type') != 'VirtualAppliance' or ip not in ipaddress.ip_network(stem+'1.0/27') or not 4 <= int(str(ip).split('.')[-1]) <= 30:
                raise ValueError('Only home/transit routes via owned NVA allowed; default routes forbidden')
            if deleting or value.get('id'):
                require_id(value.get('id'), rt+'/routes/to-'+key)
    return name
