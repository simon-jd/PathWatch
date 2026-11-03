"""Exact offline XDP plan scope. No cloud calls or live admission."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import ipaddress
import re

SCOPES = {'xdp-bootstrap', 'xdp-runner'}
GROUP = 'rg-pathwatch-lab'
IMAGE = {'publisher': 'Canonical', 'offer': 'ubuntu-24_04-lts', 'sku': 'server-arm64', 'version': '24.04.202610210'}
BOOTSTRAP = {'azurerm_resource_group.project', 'azurerm_storage_account.state', 'azurerm_role_assignment.operator_state', 'azurerm_storage_container.state'}
RUNNER = {kind + '.runner' for kind in ('azurerm_virtual_network', 'azurerm_subnet', 'azurerm_network_security_group', 'azurerm_public_ip', 'azurerm_network_interface', 'azurerm_network_interface_security_group_association', 'azurerm_linux_virtual_machine')} | {'azapi_update_resource.disk_tags'}
# Provider-computed outputs only, never a wildcard or an unknown nested block.
COMPUTED = {
    'azurerm_resource_group': {'id'},
    'azurerm_storage_account': {'id', 'primary_blob_endpoint', 'primary_blob_host', 'primary_location', 'secondary_location', 'primary_access_key', 'secondary_access_key', 'primary_connection_string', 'secondary_connection_string', 'primary_blob_connection_string', 'secondary_blob_connection_string'},
    'azurerm_storage_container': {'id', 'resource_manager_id', 'has_immutability_policy', 'has_legal_hold'},
    'azurerm_role_assignment': {'id', 'name', 'principal_type', 'role_definition_id'},
    'azurerm_virtual_network': {'id', 'guid'}, 'azurerm_subnet': {'id'},
    'azurerm_network_security_group': {'id'},
    'azurerm_public_ip': {'id', 'ip_address', 'fqdn'},
    'azurerm_network_interface': {'id', 'mac_address', 'private_ip_address', 'private_ip_addresses', 'virtual_machine_id', 'applied_dns_servers'},
    'azurerm_network_interface_security_group_association': {'id'},
    'azurerm_linux_virtual_machine': {'id', 'virtual_machine_id', 'private_ip_address', 'private_ip_addresses', 'public_ip_address', 'public_ip_addresses'},
    'azapi_update_resource': {'id', 'output'},
}
PROVIDERS = {'azurerm': 'registry.terraform.io/hashicorp/azurerm', 'azapi': 'registry.terraform.io/azure/azapi'}

BOOTSTRAP_FILES = ('main.tf', 'variables.tf', 'versions.tf', 'outputs.tf', '.terraform.lock.hcl', 'backend.azurerm.tf.example')
# Filled from the exact final reviewed bootstrap inputs, not from a mutable manifest supplied by callers.
BOOTSTRAP_HASHES = {'main.tf': '0a5f063a49aa04547a4f8006a760df4dbd3a45033666b25359bfbb61defd56cc', 'variables.tf': '4614dbb7fecdee3b1c0fd1a683ea3c62f9942076f0172e1d2645fab640a7ac2b', 'versions.tf': 'bbedd07ecd6be57dae222cf910cf42dd2b65c82524768ad87a5778b8c2649555', 'outputs.tf': '72881e6e90d0377b5e1a7f14f1266905dd2a6e94b73e7d1c93da40205e029209', '.terraform.lock.hcl': 'f1ffd85d9af48c4364aca7d1fd19a138261c5959df0b213d308ae7f618ef131d', 'backend.azurerm.tf.example': '290754bcbc78d9a2482c08acacd60a8faba072f465105e69f102650bea78ff8a'}


def money(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('Invalid money') from error
    if not result.is_finite() or result < 0:
        raise ValueError('Nonfinite/negative money')
    return result


def utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise ValueError('Strict UTC required')
    return dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)


def context(ctx):
    if ctx.get('scope') not in SCOPES:
        raise ValueError('Unknown XDP scope')
    if ctx['scope'] == 'xdp-bootstrap' and ctx.get('source_hashes') != BOOTSTRAP_HASHES:
        raise ValueError('Exact frozen bootstrap source manifest required')
    sub = ctx.get('subscription_id', '')
    if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', sub) or sub == '00000000-0000-0000-0000-000000000000':
        raise ValueError('Explicit canonical subscription required')
    if not re.fullmatch(r'[a-z0-9]{8,16}', ctx.get('run_id', '')):
        raise ValueError('Immutable run ID required')
    if ctx.get('location') != 'westus':
        raise ValueError('Only westus')
    hours = (utc(ctx['ttl']) - utc(ctx['start_utc'])).total_seconds() / 3600
    if not 0 < hours <= 2:
        raise ValueError('One <=2h deadline from bootstrap; no renewal')
    peer = ipaddress.ip_network(ctx.get('operator_ipv4_cidr', ''), strict=True)
    if peer.version != 4 or peer.prefixlen != 32 or not peer.network_address.is_global:
        raise ValueError('One global operator IPv4 /32 required')
    return f'/subscriptions/{sub}/resourceGroups/{GROUP}'


def tags(ctx, hourly):
    money(hourly)
    return {'project': 'pathwatch', 'owner': 'codex-pathwatch', 'ttl': ctx['ttl'], 'est_usd_per_hour': str(hourly)}


def expected(ctx):
    """Exact configurable attributes; generated ARM IDs keep policy inputs known."""
    prefix = context(ctx)
    arm = prefix + '/providers/'
    run = ctx['run_id']
    hourly = ctx['hourly']
    if ctx['scope'] == 'xdp-bootstrap':
        name = 'pw' + run
        account = arm + 'Microsoft.Storage/storageAccounts/' + name
        principal = ctx.get('operator_principal_id', '')
        if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', principal) or principal == '00000000-0000-0000-0000-000000000000':
            raise ValueError('Explicit operator principal required')
        if money(hourly['backend']) > Decimal('.10'):
            raise ValueError('XDP bootstrap hourly cap .10')
        common = tags(ctx, hourly['backend'])
        return {
            'azurerm_resource_group.project': {'name': GROUP, 'location': 'westus', 'tags': common},
            'azurerm_storage_account.state': {'name': name, 'resource_group_name': GROUP, 'location': 'westus', 'account_tier': 'Standard', 'account_replication_type': 'LRS', 'account_kind': 'StorageV2', 'min_tls_version': 'TLS1_2', 'https_traffic_only_enabled': True, 'shared_access_key_enabled': False, 'default_to_oauth_authentication': True, 'allow_nested_items_to_be_public': False, 'public_network_access_enabled': True, 'cross_tenant_replication_enabled': False, 'tags': common, 'network_rules': [{'default_action': 'Deny', 'bypass': ['None'], 'ip_rules': [ctx['operator_ipv4_cidr'].removesuffix('/32')]}], 'blob_properties': [{'versioning_enabled': True, 'change_feed_enabled': True, 'change_feed_retention_in_days': 7, 'delete_retention_policy': [{'days': 7}], 'container_delete_retention_policy': [{'days': 7}]}]},
            'azurerm_role_assignment.operator_state': {'scope': account, 'role_definition_name': 'Storage Blob Data Contributor', 'principal_id': principal},
            'azurerm_storage_container.state': {'name': 'tfstate', 'storage_account_id': account, 'container_access_type': 'private'},
        }
    suffix = 'pathwatch-xdp-' + run
    ids = {key: arm + resource + name + suffix for key, resource, name in [('vnet', 'Microsoft.Network/virtualNetworks/', 'vnet-'), ('nsg', 'Microsoft.Network/networkSecurityGroups/', 'nsg-'), ('pip', 'Microsoft.Network/publicIPAddresses/', 'pip-'), ('nic', 'Microsoft.Network/networkInterfaces/', 'nic-'), ('vm', 'Microsoft.Compute/virtualMachines/', 'vm-'), ('disk', 'Microsoft.Compute/disks/', 'os-')]}
    key = ctx.get('admin_public_key', '')
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]{60,100}={0,2}(?: [^\r\n]+)?', key):
        raise ValueError('Explicit Ed25519 public key required')
    def parent(name, rate='0'):
        return {'name': name + suffix, 'resource_group_name': GROUP, 'location': 'westus', 'tags': tags(ctx, rate)}
    return {
        'azurerm_virtual_network.runner': parent('vnet-') | {'address_space': ['10.203.0.0/24'], 'dns_servers': []},
        'azurerm_subnet.runner': {'name': 'runner', 'resource_group_name': GROUP, 'virtual_network_name': 'vnet-' + suffix, 'address_prefixes': ['10.203.0.0/27']},
        'azurerm_network_security_group.runner': parent('nsg-') | {'security_rule': [{'name': 'operator-ssh', 'priority': 100, 'direction': 'Inbound', 'access': 'Allow', 'protocol': 'Tcp', 'source_port_range': '*', 'destination_port_range': '22', 'source_address_prefix': ctx['operator_ipv4_cidr'], 'destination_address_prefix': '10.203.0.4'}]},
        'azurerm_public_ip.runner': parent('pip-', hourly['ip']) | {'allocation_method': 'Static', 'sku': 'Standard', 'sku_tier': 'Regional', 'ip_version': 'IPv4'},
        'azurerm_network_interface.runner': parent('nic-') | {'ip_forwarding_enabled': False, 'accelerated_networking_enabled': False, 'dns_servers': [], 'ip_configuration': [{'name': 'primary', 'primary': True, 'private_ip_address_allocation': 'Static', 'private_ip_address': '10.203.0.4', 'subnet_id': ids['vnet'] + '/subnets/runner', 'public_ip_address_id': ids['pip']}]},
        'azurerm_network_interface_security_group_association.runner': {'network_interface_id': ids['nic'], 'network_security_group_id': ids['nsg']},
        'azurerm_linux_virtual_machine.runner': parent('vm-', hourly['vm']) | {'size': 'Standard_B2ps_v2', 'admin_username': 'pathwatch', 'disable_password_authentication': True, 'network_interface_ids': [ids['nic']], 'provision_vm_agent': True, 'allow_extension_operations': False, 'patch_mode': 'ImageDefault', 'priority': 'Regular', 'admin_ssh_key': [{'username': 'pathwatch', 'public_key': key}], 'os_disk': [{'name': 'os-' + suffix, 'caching': 'ReadWrite', 'storage_account_type': 'Standard_LRS', 'disk_size_gb': 32}], 'source_image_reference': [IMAGE]},
        'azapi_update_resource.disk_tags': {'type': 'Microsoft.Compute/disks@2024-03-02', 'resource_id': ids['disk'], 'body': {'tags': tags(ctx, hourly['disk'])}},
    }


def resource_id(address, value, ctx):
    base = context(ctx)
    kind = address.split('.')[0]
    if kind == 'azurerm_resource_group':
        return base
    if kind == 'azapi_update_resource':
        return value['resource_id']
    if kind == 'azurerm_subnet':
        return base + '/providers/Microsoft.Network/virtualNetworks/' + value['virtual_network_name'] + '/subnets/runner'
    if kind == 'azurerm_network_interface_security_group_association':
        return value['network_interface_id'] + '|' + value['network_security_group_id']
    if kind == 'azurerm_storage_container':
        return value['storage_account_id'] + '/blobServices/default/containers/tfstate'
    if kind == 'azurerm_role_assignment':
        return None  # UUID is provider-computed; validate exact account scope below.
    segment = {'azurerm_storage_account': 'Microsoft.Storage/storageAccounts', 'azurerm_virtual_network': 'Microsoft.Network/virtualNetworks', 'azurerm_network_security_group': 'Microsoft.Network/networkSecurityGroups', 'azurerm_public_ip': 'Microsoft.Network/publicIPAddresses', 'azurerm_network_interface': 'Microsoft.Network/networkInterfaces', 'azurerm_linux_virtual_machine': 'Microsoft.Compute/virtualMachines'}[kind]
    return base + '/providers/' + segment + '/' + value['name']


def compare(actual, wanted, path=''):
    """Permit only absent/null/empty provider defaults beyond declared inputs."""
    if isinstance(wanted, dict):
        if not isinstance(actual, dict):
            raise ValueError('Missing configuration at ' + path)
        for key, value in wanted.items():
            compare(actual.get(key), value, path + '.' + key)
        for key in set(actual) - set(wanted):
            if actual[key] not in (None, [], {}):
                raise ValueError('Unreviewed attribute at ' + path + '.' + key)
    elif isinstance(wanted, list):
        if not isinstance(actual, list) or len(actual) != len(wanted):
            raise ValueError('Unexpected block count at ' + path)
        for i, (a, w) in enumerate(zip(actual, wanted)):
            compare(a, w, path + f'[{i}]')
    elif type(actual) is not type(wanted) or actual != wanted:
        raise ValueError('Configuration mismatch at ' + path)


def unknown_paths(value, path=()):
    if value is True:
        yield path
    elif value is False:
        return
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from unknown_paths(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from unknown_paths(child, path + (index,))
    else:
        raise ValueError('Malformed unknown mask')


def validate(plan, subscription, ctx, deleting=False):
    wanted = expected(ctx)
    if plan.get('deferred_changes') or plan.get('resource_drift'):
        raise ValueError('Deferred/drifting plan is not a frozen scope')
    variables = {k: v.get('value') for k, v in plan.get('variables', {}).items()}
    if subscription != ctx['subscription_id'] or variables.get('subscription_id') != subscription or variables.get('location') != 'westus' or variables.get('ttl') != ctx['ttl']:
        raise ValueError('Plan subscription/location/TTL mismatch')
    if ctx['scope'] == 'xdp-runner' and (variables.get('xdp_scope') != 'xdp-runner' or variables.get('run_id') != ctx['run_id']):
        raise ValueError('Runner scope/run mismatch')
    config = plan.get('configuration', {})
    root = config.get('root_module', {})
    if root.get('module_calls'):
        raise ValueError('No child modules in exact XDP root')
    entries = root.get('resources', [])
    configured = {r.get('address'): r for r in entries}
    if len(configured) != len(entries) or set(configured) != set(wanted):
        raise ValueError('Exact configuration addresses required')
    provider_configs = config.get('provider_config', {})
    provider_names = {'azurerm', 'azapi'} if ctx['scope'] == 'xdp-runner' else {'azurerm'}
    if set(provider_configs) != provider_names:
        raise ValueError('Unexpected provider configurations')
    # Live provider behavior also requires the frozen HCL/lock manifest and native validation.
    for name, item in provider_configs.items():
        if item.get('full_name') != PROVIDERS[name] or item.get('alias'):
            raise ValueError('Provider substitution')
        allowed = {'subscription_id', 'resource_provider_registrations', 'storage_use_azuread', 'features'} if name == 'azurerm' else {'subscription_id', 'skip_provider_registration', 'enable_preflight'}
        expressions = item.get('expressions', {})
        if set(expressions) - allowed or expressions.get('subscription_id') != {'references': ['var.subscription_id']}:
            raise ValueError('Provider configuration mutation')
        fixed = {'resource_provider_registrations': 'none'} if name == 'azurerm' else {'skip_provider_registration': True, 'enable_preflight': False}
        if name == 'azurerm':
            fixed['features'] = [{'virtual_machine': [{'delete_os_disk_on_deletion': True}]}] if ctx['scope'] == 'xdp-runner' else [{}]
            if ctx['scope'] == 'xdp-bootstrap':
                fixed['storage_use_azuread'] = True
        if set(expressions) != set(fixed) | {'subscription_id'}:
            raise ValueError('Provider configuration mutation')
        for field, value in fixed.items():
            if expressions.get(field) != {'constant_value': value}:
                raise ValueError('Provider registration/preflight mutation')
    seen = set()
    for resource in plan.get('resource_changes', []):
        address = resource.get('address')
        kind = resource.get('type')
        if resource.get('mode') != 'managed' or address not in wanted or address in seen or kind != address.split('.')[0]:
            raise ValueError('Mixed/extra/duplicate resource scope')
        seen.add(address)
        if resource.get('provider_name') != PROVIDERS['azapi' if kind == 'azapi_update_resource' else 'azurerm']:
            raise ValueError('Resource provider mutation')
        change = resource['change']
        if change.get('actions') not in ([['delete'], ['no-op']] if deleting else [['create']]):
            raise ValueError('Only create or reviewed delete/no-op')
        if not deleting and change.get('before') is not None:
            raise ValueError('Create cannot adopt prior state')
        if deleting and change['actions'] == ['no-op'] and change.get('after') != change.get('before'):
            raise ValueError('No-op values differ')
        value = change.get('before' if deleting else 'after')
        if not isinstance(value, dict):
            raise ValueError('Missing before/after values')
        unknown = change.get('after_unknown', {})
        if not isinstance(unknown, dict):
            raise ValueError('Unknown critical values')
        for path in unknown_paths(unknown):
            if deleting or len(path) != 1 or path[0] not in COMPUTED[kind]:
                raise ValueError('Unknown critical attribute: ' + str(path))
        expressions = configured[address].get('expressions', {})
        if set(expressions) - set(wanted[address]):
            raise ValueError('Unreviewed configured attribute (including write-only)')
        filtered = {k: v for k, v in value.items() if k not in COMPUTED[kind]}
        compare(filtered, wanted[address], address)
        rid = value.get('id')
        if deleting or rid is not None:
            target = resource_id(address, wanted[address], ctx)
            if kind == 'azurerm_role_assignment':
                if not re.fullmatch(re.escape(value['scope']) + r'/providers/Microsoft.Authorization/roleAssignments/[0-9a-f-]{36}', rid or ''):
                    raise ValueError('Role ID outside owned account')
            elif rid != target:
                raise ValueError('Before/current ID outside exact run')
    if (not deleting and seen != set(wanted)) or not seen:
        raise ValueError('Incomplete XDP create scope')
    return wanted
