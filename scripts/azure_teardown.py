#!/usr/bin/env python3
"""Validate reviewed delete-only identity/network plans; retain shared backend/RG."""
import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

# Resolve the adjacent reviewed helper even when this script is loaded by a
# test worker outside the checkout (without changing sys.path or credentials).
_network_spec = importlib.util.spec_from_file_location('azure_network_scope', Path(__file__).with_name('azure_network_scope.py'))
network_scope = importlib.util.module_from_spec(_network_spec)
_network_spec.loader.exec_module(network_scope)

_nva_spec = importlib.util.spec_from_file_location('azure_nva_scope', Path(__file__).with_name('azure_nva_scope.py'))
nva_scope = importlib.util.module_from_spec(_nva_spec)
_nva_spec.loader.exec_module(nva_scope)

_dns_spec = importlib.util.spec_from_file_location('azure_dns_scope', Path(__file__).with_name('azure_dns_scope.py'))
dns_scope = importlib.util.module_from_spec(_dns_spec)
_dns_spec.loader.exec_module(dns_scope)
_probe_spec = importlib.util.spec_from_file_location('azure_probe_scope', Path(__file__).with_name('azure_probe_scope.py'))
probe_scope = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(probe_scope)

_xdp_spec = importlib.util.spec_from_file_location('azure_xdp_lifecycle', Path(__file__).with_name('azure_xdp_lifecycle.py'))
xdp_lifecycle = importlib.util.module_from_spec(_xdp_spec)
_xdp_spec.loader.exec_module(xdp_lifecycle)

ADDRESSES = {
    'module.iam_oidc.azurerm_user_assigned_identity.ci': 'azurerm_user_assigned_identity',
    'module.iam_oidc.azurerm_federated_identity_credential.github': 'azurerm_federated_identity_credential',
    'module.iam_oidc.azurerm_role_assignment.state': 'azurerm_role_assignment',
}


def principal_uuid(value):
    """Canonical Azure object UUID, rejecting missing/empty/malformed/nil principals."""
    if not isinstance(value, str) or not re.fullmatch(
            r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', value):
        raise ValueError('Missing/invalid principal UUID; reconcile owned identity state and replan')
    parsed = uuid.UUID(value)
    if parsed.int == 0:
        raise ValueError('Nil principal UUID cannot prove identity ownership; reconcile state and replan')
    return str(parsed)


def validate(plan, subscription, xdp_receipt=None):
    if xdp_receipt is not None:
        return xdp_lifecycle.validate_teardown(plan, subscription, xdp_receipt)
    if "xdp_scope" in plan.get("variables", {}):
        raise ValueError("Explicit XDP teardown receipt required")
    if probe_scope.matches(plan):
        probe_scope.validate(plan, subscription, deleting=True)
        return
    if dns_scope.is_dns_plan(plan):
        dns_scope.validate(plan, subscription, deleting=True)
        return
    if nva_scope.is_nva_plan(plan):
        nva_scope.validate(plan, subscription, network_scope, deleting=True)
        return
    if network_scope.is_network_plan(plan):
        network_scope.validate(plan, subscription, deleting=True)
        return
    if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', subscription):
        raise ValueError('Explicit AZURE_SUBSCRIPTION_ID required')
    if plan.get('variables', {}).get('subscription_id', {}).get('value') != subscription:
        raise ValueError('Plan subscription mismatch')
    prefix = f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab'
    identity = prefix+'/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-pathwatch-github'
    storage = plan.get('variables', {}).get('state_storage_account_id', {}).get('value', '')
    if not re.fullmatch(re.escape(prefix)+r'/providers/Microsoft.Storage/storageAccounts/[a-z0-9]+', storage, re.I):
        raise ValueError('State scope outside exact project storage account')
    seen = set()
    ci_principal = None
    role_principal = None
    for resource in plan.get('resource_changes', []):
        if resource.get('mode') != 'managed':
            continue
        address = resource['address']
        if address not in ADDRESSES or resource['type'] != ADDRESSES[address] or address in seen:
            raise ValueError('Only the three known global identity resources may be torn down')
        seen.add(address)
        change = resource['change']
        if change['actions'] not in (['delete'], ['no-op']):
            raise ValueError('Teardown refuses creates, updates and replacements')
        before = change.get('before') or {}
        rid = before.get('id', '')
        typ = resource['type']
        if typ == 'azurerm_user_assigned_identity':
            if rid.lower() != identity.lower() or before.get('resource_group_name') != 'rg-pathwatch-lab':
                raise ValueError('Identity scope mismatch')
            tags = before.get('tags') or {}
            if tags.get('project') != 'pathwatch' or tags.get('owner') != 'codex-pathwatch':
                raise ValueError('Identity ownership mismatch')
            ci_principal = principal_uuid(before.get('principal_id'))
        elif typ == 'azurerm_federated_identity_credential':
            if (before.get('parent_id', '').lower() != identity.lower()
                    or rid.lower() != (identity+'/federatedIdentityCredentials/github-main').lower()):
                raise ValueError('Federated credential parent/scope mismatch')
        else:
            if (before.get('scope', '').lower() != storage.lower()
                    or before.get('role_definition_name') != 'Storage Blob Data Contributor'
                    or not re.fullmatch(re.escape(storage)+r'/providers/Microsoft.Authorization/roleAssignments/[0-9a-f-]{36}', rid, re.I)):
                raise ValueError('Role assignment scope mismatch')
            role_principal = principal_uuid(before.get('principal_id'))
    # Terraform resource_changes ordering is not a trust boundary: compare only
    # after the owned UAMI before-state has itself passed scope/tag validation.
    if role_principal is not None:
        if ci_principal is None:
            raise ValueError('Cannot prove role principal ownership: reconcile/import the owned CI identity into global state and replan')
        if role_principal != ci_principal:
            raise ValueError('Role principal differs from the owned CI identity principal; reconcile global state and replan')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--reviewed-sha256', required=True)
    parser.add_argument('--xdp-receipt')
    args = parser.parse_args()
    path = Path(args.plan).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not re.fullmatch(r'[0-9a-f]{64}', args.reviewed_sha256) or digest != args.reviewed_sha256:
        raise ValueError('Supply the exact reviewed destroy plan SHA256 before teardown')
    if args.xdp_receipt:
        plan = xdp_lifecycle.inspect_plan(path, args.reviewed_sha256)
        receipt = xdp_lifecycle.read_private(args.xdp_receipt)
        validate(plan, os.environ.get('AZURE_SUBSCRIPTION_ID', ''), receipt)
        print('OFFLINE XDP receipt validation only; LIVE teardown execution PENDING')
        return
    result = subprocess.run(['terraform', 'show', '-json', str(path)], cwd=path.parent,
                            text=True, capture_output=True, check=True)
    validate(json.loads(result.stdout), os.environ.get('AZURE_SUBSCRIPTION_ID', ''))
    print('PASS: reviewed project identity/network deletion only; RG and backend are retained')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f'TEARDOWN REFUSED: {error}', file=sys.stderr)
        sys.exit(1)
