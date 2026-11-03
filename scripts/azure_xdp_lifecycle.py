"""Private prepare and offline receipt checks only. No apply/Azure/reaper execution."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess

_spec = importlib.util.spec_from_file_location('xdp_lifecycle_scope', Path(__file__).with_name('azure_xdp_scope.py'))
scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope)
ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_FILES = scope.BOOTSTRAP_FILES
BOOTSTRAP_HASHES = scope.BOOTSTRAP_HASHES


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_parent(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise ValueError('Existing private0700 owned directory required')


def private_bytes(path):
    path = Path(path)
    private_parent(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_size > 1048576:
            raise ValueError('Private bounded0600 single-link file required')
        payload = stream.read(1048577)
        if len(payload) > 1048576:
            raise ValueError('Oversized private receipt')
        return payload


def read_private(path):
    return json.loads(private_bytes(path))


def create_private(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(parent, ctx):
    """Create once; preserve partial output on failure, never reset an old lineage."""
    scope.context(ctx)
    if ctx['scope'] != 'xdp-bootstrap':
        raise ValueError('Only new bootstrap staging')
    private_parent(parent)
    source = ROOT / 'infra/bootstrap'
    files = {name: (source / name).read_bytes() for name in BOOTSTRAP_FILES}
    if {name: digest(data) for name, data in files.items()} != BOOTSTRAP_HASHES:
        raise ValueError('Bootstrap source differs from frozen manifest')
    target = Path(parent) / ctx['run_id']
    target.mkdir(mode=0o700)  # create-only marker; errors never delete this directory
    (target / 'tf-data').mkdir(mode=0o700)
    for name, data in files.items():
        create_private(target / name, data)
    variables = {'subscription_id': ctx['subscription_id'], 'location': 'westus', 'ttl': ctx['ttl'], 'est_usd_per_hour': str(scope.money(ctx['hourly']['backend'])), 'storage_account_name': 'pw' + ctx['run_id'], 'operator_principal_id': ctx['operator_principal_id'], 'operator_ipv4_cidrs': [ctx['operator_ipv4_cidr']]}
    scope.expected(ctx)
    create_private(target / 'run.tfvars.json', json.dumps(variables, indent=2).encode())
    receipt = {'schema': 1, 'context': ctx, 'source_hashes': BOOTSTRAP_HASHES, 'bootstrap_key': ctx['run_id'] + '/bootstrap.tfstate', 'runner_key': ctx['run_id'] + '/runner.tfstate', 'lineage': 'NEW: Terraform has not initialized state', 'live_execution': 'PENDING'}
    create_private(target / 'run.json', json.dumps(receipt, indent=2).encode())
    sync_directory(target)
    sync_directory(parent)
    return target


def state_metadata(record):
    payload = private_bytes(record['path'])
    if digest(payload) != record.get('sha256'):
        raise ValueError('Export/state hash changed')
    data = json.loads(payload)
    lineage, serial = data.get('lineage'), data.get('serial')
    if not isinstance(lineage, str) or not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', lineage) or type(serial) is not int or serial < 0:
        raise ValueError('Invalid state lineage/serial')
    resources = data.get('resources')
    if not isinstance(resources, list):
        raise ValueError('Missing state resource collection')
    count = sum(len(r['instances']) for r in resources if r.get('mode') == 'managed')
    return (lineage, serial, count), data


def verify_migration(receipt):
    exports = receipt.get('exports', {})
    if set(exports) != {'bootstrap_remote', 'bootstrap_local', 'runner_empty'}:
        raise ValueError('Backend deletion requires private exports and migration receipt')
    if len({str(Path(r['path']).resolve()) for r in exports.values()}) != 3:
        raise ValueError('Independent remote/local/runner state files required')
    ctx = receipt['context']
    expected = scope.expected(ctx)
    verified_states = {label: state_metadata(record) for label, record in exports.items()}
    identities = {}
    for label in ('bootstrap_remote', 'bootstrap_local'):
        data = verified_states[label][1]
        identities[label] = {}
        managed = [r for r in data.get('resources', []) if r.get('mode') == 'managed']
        if len(managed) != 4 or any(r.get('module') for r in managed) or {r['type'] + '.' + r['name'] for r in managed} != scope.BOOTSTRAP:
            raise ValueError('Exports do not contain this exact bootstrap root')
        for resource in managed:
            address = resource['type'] + '.' + resource['name']
            if len(resource['instances']) != 1:
                raise ValueError('Unexpected bootstrap instances')
            attributes = resource['instances'][0]['attributes']
            scope.compare({k: v for k, v in attributes.items() if k not in scope.COMPUTED[resource['type']]}, expected[address], address)
            rid = scope.resource_id(address, expected[address], ctx)
            if rid is not None and attributes.get('id') != rid:
                raise ValueError('Export belongs to another run/subscription')
            if rid is None and not re.fullmatch(re.escape(expected[address]['scope']) + r'/providers/Microsoft.Authorization/roleAssignments/[0-9a-f-]{36}', attributes.get('id', '')):
                raise ValueError('Export role scope mismatch')
            identities[label][address] = attributes['id']
    if identities['bootstrap_remote'] != identities['bootstrap_local']:
        raise ValueError('Migration resource identities differ between exports')
    remote = verified_states['bootstrap_remote'][0]
    local = verified_states['bootstrap_local'][0]
    runner = verified_states['runner_empty'][0]
    if remote != local or remote[2] != 4 or runner[2] != 0 or remote[0] == runner[0]:
        raise ValueError('Migration lineage/serial/count mismatch')
    # Verify bytes are durable now; a boolean claiming fsync is not evidence.
    for record in exports.values():
        fd = os.open(record['path'], os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory(Path(record['path']).parent)
    return {'state_files_match': True, 'resource_ids': identities['bootstrap_local'], 'live_lease_and_remote_migration_verification': 'PENDING'}


def validate_teardown(plan, subscription, receipt):
    ctx = receipt['context']
    if receipt.get('scope') != ctx.get('scope'):
        raise ValueError('Explicit teardown scope mismatch')
    scope.validate(plan, subscription, ctx, deleting=True)
    if ctx['scope'] == 'xdp-bootstrap':
        identities = verify_migration(receipt)['resource_ids']
        for resource in plan['resource_changes']:
            if resource['change']['before']['id'] != identities[resource['address']]:
                raise ValueError('Teardown identity differs from verified migration exports')
    return {'offline_only': True, 'execution': 'PENDING: actual migration/lease, inventory, watcher and cleanup execution need separate review'}


def inspect_plan(path, expected_sha):
    """Real terraform-show boundary, same reviewed binary before and after render."""
    path = Path(path).resolve()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha or '') or digest(path.read_bytes()) != expected_sha:
        raise ValueError('Reviewed binary-plan SHA mismatch')
    rendered = subprocess.run(['terraform', 'show', '-json', str(path)], cwd=path.parent, capture_output=True, text=True, check=True, timeout=60)
    if digest(path.read_bytes()) != expected_sha:
        raise ValueError('Plan changed while rendering')
    return json.loads(rendered.stdout)


def cleanup_candidates(receipt, inventory, now):
    """Produce exact IDs for review; NEVER execute Azure commands or trust tags alone."""
    ctx = receipt['context']
    if receipt.get('scope') != ctx.get('scope'):
        raise ValueError('Cleanup scope mismatch')
    expected = scope.expected(ctx)
    if scope.utc(ctx['ttl']) > now:
        raise ValueError('Run is not expired')
    if ctx['scope'] == 'xdp-bootstrap':
        identities = verify_migration(receipt)['resource_ids']
    if not isinstance(inventory, list):
        raise ValueError('Exact owned inventory list required')
    wanted = {scope.resource_id(a, v, ctx): (a, v) for a, v in expected.items() if scope.resource_id(a, v, ctx) is not None}
    if ctx['scope'] == 'xdp-bootstrap':
        # The generated role UUID is known only from the already verified export.
        # Do not accept any other role merely because its account scope matches.
        address = 'azurerm_role_assignment.operator_state'
        wanted[identities[address]] = (address, expected[address])
    ids = []
    for actual in inventory:
        rid = actual.get('id')
        if rid not in wanted or rid in ids:
            raise ValueError('Foreign/duplicate inventory; no broad sweep')
        address, value = wanted[rid]
        kind = address.split('.')[0]
        required_tags = value.get('body', {}).get('tags', value.get('tags'))
        if required_tags is not None and actual.get('tags') != required_tags:
            # Untagged disk after partial tag-PATCH failure: require matching exact
            # owned VM storageProfile ID, not a guessed name or "approved" flag.
            vm = next((r for r in inventory if r.get('id') == scope.resource_id('azurerm_linux_virtual_machine.runner', expected.get('azurerm_linux_virtual_machine.runner', {}), ctx)), None) if ctx['scope'] == 'xdp-runner' else None
            if kind != 'azapi_update_resource' or actual.get('tags') not in (None, {}) or vm is None or vm.get('tags') != expected['azurerm_linux_virtual_machine.runner']['tags'] or vm.get('storageProfile', {}).get('osDisk', {}).get('managedDisk', {}).get('id') != rid:
                raise ValueError('Partial-apply ownership cannot be established')
        ids.append(rid)
    return {'offline_only': True, 'candidate_ids': ids, 'execution': 'PENDING: live XDP reaper/watchers not wired; partial cleanup must be reconciled'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_args = commands.add_parser('prepare')
    prepare_args.add_argument('--parent', required=True)
    prepare_args.add_argument('--context', required=True)
    cleanup = commands.add_parser('xdp-check')
    cleanup.add_argument('--receipt', required=True)
    cleanup.add_argument('--inventory', required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        target = prepare(Path(args.parent), read_private(args.context))
        print('OFFLINE private staging only:', target)
    else:
        result = cleanup_candidates(read_private(args.receipt), read_private(args.inventory), dt.datetime.now(dt.timezone.utc))
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print('XDP REFUSED:', error)
        raise SystemExit(1)
