"""Offline XDP cost review. Production admission remains unconditionally pending."""
import importlib.util
import json
import hashlib
from pathlib import Path
from decimal import Decimal, ROUND_CEILING

_spec = importlib.util.spec_from_file_location('xdp_scope', Path(__file__).with_name('azure_xdp_scope.py'))
scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope)
# Whole campaign quantities, including all backend requests and retained versions.
METERS = {
    'vm_hours': ('Virtual Machines Bpsv2 Series', 'B2ps v2', 'B2ps v2', '1 Hour', Decimal(2)),
    'ip_hours': ('IP Addresses', 'Standard', 'Standard IPv4 Static Public IP', '1 Hour', Decimal(2)),
    'disk_month': ('Standard HDD Managed Disks', 'S4 LRS', 'S4 LRS Disk', '1/Month', Decimal(2) / 730),
    'disk_transactions': ('Standard HDD Managed Disks', 'S4 LRS', 'S4 LRS Disk Operations', '10K', Decimal(2000)),
    'aggregate_egress_gb': ('Rtn Preference: MGN', 'Standard', 'Standard Data Transfer Out', '1 GB', Decimal(2)),
    'backend_gb_month': ('General Block Blob v2', 'Hot LRS', 'Hot LRS Data Stored', '1 GB/Month', Decimal(1)),
    'backend_write_10k': ('General Block Blob v2', 'Hot LRS', 'Hot LRS Write Operations', '10K', Decimal(1)),
    'backend_read_10k': ('General Block Blob v2', 'Hot LRS', 'Hot Read Operations', '10K', Decimal(1)),
    'backend_list_10k': ('General Block Blob v2', 'Hot LRS', 'LRS List and Create Container Operations', '10K', Decimal(1)),
    'backend_other_10k': ('General Block Blob v2', 'Hot LRS', 'All Other Operations', '10K', Decimal(1)),
}
PENDING = 'PENDING: XDP live admission/lifecycle, native validation, image minimum, SSH, watcher and tool proofs are not implemented; no reservation permitted'


def claimed(plan, proposal):
    return 'scope' in proposal or 'xdp_scope' in plan.get('variables', {})


def check(plan, proposal, ledger, now, api):
    if proposal.get('scope') not in scope.SCOPES:
        raise ValueError('Unknown explicit scope')
    # No externally supplied approval booleans or synthetic flag can bypass this.
    raise ValueError(PENDING)


def preflight(receipt, ctx, now):
    if receipt.get('subscription_id') != ctx['subscription_id'] or receipt.get('location') != 'westus':
        raise ValueError('Capability subscription/region mismatch')
    age = now - scope.utc(receipt['captured_at'])
    if not 0 <= age.total_seconds() <= 86400:
        raise ValueError('Stale/future capability receipt')
    if 'westus' not in receipt.get('allowed_regions', []):
        raise ValueError('Region policy excludes westus')
    sku = receipt.get('sku', {})
    if sku.get('name') != 'Standard_B2ps_v2' or sku.get('restrictions') != [] or sku.get('location') != 'westus' or sku.get('architecture') != 'Arm64' or sku.get('vcpus') != 2 or sku.get('memory_gib') != 8:
        raise ValueError('Exact unrestricted ARM SKU required')
    for name in ['cores', 'standardBpsv2Family']:
        q = receipt.get('quota', {}).get(name, {})
        current, limit = q.get('current'), q.get('limit')
        if type(current) is not int or type(limit) is not int or not 0 <= current <= limit or current + 2 > limit:
            raise ValueError('Quota missing/exhausted')
        if name == 'cores' and current + 2 > 6:
            raise ValueError('User regional six-vCPU cap')
    image = receipt.get('image', {})
    if image.get('urn') != 'Canonical:ubuntu-24_04-lts:server-arm64:24.04.202610210' or image.get('architecture') != 'Arm64' or image.get('hyperv') != 'V2' or image.get('plan') is not None or 'plan' not in image:
        raise ValueError('Pinned free ARM Gen2 image required')
    size = image.get('minimum_disk_gib')
    if type(size) is not int or not 0 < size <= 32:
        raise ValueError('Image32GiB minimum unknown/incompatible')


def evaluate(plan, proposal, ledger, now, api):
    """Pure fixture/offline evaluation, never used by live reserve/check."""
    api.enforce_reconciliation(ledger)
    ctx = proposal['context']
    if proposal.get('scope') != ctx.get('scope'):
        raise ValueError('Proposal/context scope mismatch')
    values = scope.validate(plan, ctx['subscription_id'], ctx)
    if (scope.utc(ctx['ttl']) - scope.utc(ctx['start_utc'])).total_seconds() != 7200 or not scope.utc(ctx['start_utc']) <= now < scope.utc(ctx['ttl']):
        raise ValueError('This cost profile freezes one active two-hour campaign')
    preflight(proposal['capability'], ctx, now)
    pricing = proposal['pricing']
    if set(pricing) != set(METERS):
        raise ValueError('Every campaign meter exactly once, including disk child/backend')
    costs = {}
    for category, (product, sku, meter, unit, quantity) in METERS.items():
        record = pricing[category]
        selector = record['selector']
        if tuple(selector.get(k) for k in ('productName', 'skuName', 'meterName', 'unitOfMeasure')) != (product, sku, meter, unit) or selector.get('armRegionName') != 'westus' or scope.money(record['quantity']) != quantity:
            raise ValueError('Meter/quantity mismatch: ' + category)
        path = api.ROOT / record['capture']
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != record.get('sha256'):
            raise ValueError('Price capture changed')
        price = api.validate_meter(json.loads(payload), selector, now)
        costs[category] = (price * quantity).quantize(Decimal('.01'), rounding=ROUND_CEILING)
    assignments = {
        'azurerm_storage_account.state': {k for k in METERS if k.startswith('backend_')},
        'azurerm_linux_virtual_machine.runner': {'vm_hours', 'aggregate_egress_gb'},
        'azurerm_public_ip.runner': {'ip_hours'},
        'azapi_update_resource.disk_tags': {'disk_month', 'disk_transactions'},
    }
    rows = proposal['resources']
    by = {row['address']: row for row in rows}
    if len(by) != len(rows) or set(by) != set(values):
        raise ValueError('Exact resource ledger rows required')
    for address, value in values.items():
        row = by[address]
        categories = assignments.get(address, set())
        estimate = sum((costs[k] for k in categories), Decimal(0))
        if row.get('categories') != sorted(categories) or scope.money(row['estimate_usd']) != estimate or row.get('type') != address.split('.')[0] or row.get('region') != 'westus' or row.get('start_utc') != ctx['start_utc'] or row.get('end_utc') != ctx['ttl'] or row.get('same_session_delete') is not True or row.get('run_id') != ctx['run_id']:
            raise ValueError('Missing/duplicated/underpriced row or lifecycle bound')
        tag = value.get('body', {}).get('tags', value.get('tags', {}))
        if categories and scope.money(tag.get('est_usd_per_hour', 'NaN')) < estimate / 2:
            raise ValueError('Hourly tag understates category-rounded cost')
    total = sum(costs.values())
    # Conservative full campaign check even when reviewing only one root.
    api.enforce_cap(api.total_ledger(ledger), total)
    return {'scope': ctx['scope'], 'rows': rows, 'category_costs': {k: str(v) for k, v in costs.items()}, 'campaign_usd': str(total), 'live_admission': False, 'pending': PENDING}


def ordinary_bootstrap_cap(plan):
    """Keep the old .02 HCL+guard policy outside explicit XDP scope (R8b)."""
    managed = [r for r in plan.get('resource_changes', []) if r.get('mode') == 'managed']
    if managed and {r.get('address') for r in managed} <= scope.BOOTSTRAP:
        for row in managed:
            if row.get('address') == 'azurerm_storage_account.state' and row.get('type') == 'azurerm_storage_account' and row['change']['actions'] == ['create']:
                rate = row['change'].get('after', {}).get('tags', {}).get('est_usd_per_hour', 'NaN')
                if scope.money(rate) > Decimal('.02'):
                    raise ValueError('Ordinary bootstrap hourly cap remains .02; explicit XDP scope required')
