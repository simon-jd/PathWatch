#!/usr/bin/env python3
"""Copy a network-only local-state fixture for manual plan/review/reservation.

No cloud commands, credentials, state, backend metadata or provider binaries are
copied. The integration test will refuse any run outside this ignored directory.
"""
import argparse
import datetime as dt
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {'westus', 'francecentral', 'norwayeast', 'denmarkeast', 'swedencentral'}


def prepare(subscription, location, address_space, ttl, peers):
    if str(uuid.UUID(subscription)) != subscription or uuid.UUID(subscription).int == 0:
        raise ValueError('Explicit canonical non-nil subscription UUID required')
    if location not in ALLOWED or address_space not in {'10.10.0.0/16', '10.20.0.0/16'}:
        raise ValueError('Choose an authorized region and assigned /16')
    expiry = dt.datetime.strptime(ttl, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    remaining = expiry-dt.datetime.now(dt.timezone.utc)
    if not dt.timedelta(0) < remaining <= dt.timedelta(hours=4):
        raise ValueError('Integration fixture TTL must expire within four hours')
    if not peers:
        raise ValueError('Supply at least one actual owned public WireGuard peer IPv4 /32')
    for peer in peers:
        network = ipaddress.ip_network(peer, strict=True)
        if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError('WireGuard peer must be an actual owned global IPv4 /32')
    name = 'pathwatch-it-'+uuid.uuid4().hex[:12]
    run = ROOT/'.cache'/'azure-network-tests'/name
    fixture = run/'infra'/'test'/'fixtures'/'azure'
    manifest = {'name': name, 'subscription_id': subscription, 'location': location,
                'address_space': address_space, 'ttl': ttl, 'wireguard_peer_cidrs': sorted(set(peers)), 'files': {}}
    for relative in ('infra/modules/network-azure', 'infra/test/fixtures/azure'):
        source = ROOT/relative
        target = run/relative
        target.mkdir(parents=True, exist_ok=False)
        for path in source.iterdir():
            if path.is_file() and (path.suffix == '.tf' or path.name in {'.terraform.lock.hcl', 'Makefile'}):
                shutil.copyfile(path, target/path.name)
                manifest['files'][str((target/path.name).relative_to(run))] = hashlib.sha256(path.read_bytes()).hexdigest()
    values = {key: manifest[key] for key in ('name', 'subscription_id', 'location', 'address_space', 'ttl', 'wireguard_peer_cidrs')}
    (fixture/'terraform.tfvars.json').write_text(json.dumps(values, indent=2)+'\n')
    manifest['files'][str((fixture/'terraform.tfvars.json').relative_to(run))] = hashlib.sha256((fixture/'terraform.tfvars.json').read_bytes()).hexdigest()
    (run/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return run, fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subscription', required=True)
    parser.add_argument('--location', choices=sorted(ALLOWED), required=True)
    parser.add_argument('--address-space', choices=['10.10.0.0/16', '10.20.0.0/16'], required=True)
    parser.add_argument('--ttl', required=True)
    parser.add_argument('--wireguard-peer', action='append', required=True,
                        help='Actual owned public IPv4 /32; no connectivity is created')
    args = parser.parse_args()
    run, fixture = prepare(args.subscription, args.location, args.address_space, args.ttl, args.wireguard_peer)
    print(json.dumps({'run_dir': str(run), 'fixture_dir': str(fixture),
                      'next': 'Use fixture Makefile with ROOT=checkout, VARS=terraform.tfvars.json; plan, review, price-free proposal, reserve before opting in.'}, indent=2))


if __name__ == '__main__':
    main()
