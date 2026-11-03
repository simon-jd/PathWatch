"""Identity-only teardown checks and Make routing; fake Terraform, no cloud calls."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SUB = '11111111-1111-1111-1111-111111111111'
CI_PRINCIPAL = 'aabbccdd-1122-3344-5566-778899aabbcc'
spec = importlib.util.spec_from_file_location('teardown', ROOT/'scripts/azure_teardown.py')
teardown = importlib.util.module_from_spec(spec)
spec.loader.exec_module(teardown)


class TeardownTests(unittest.TestCase):
    def setUp(self):
        self.prefix = f'/subscriptions/{SUB}/resourceGroups/rg-pathwatch-lab'
        identity = self.prefix+'/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-pathwatch-github'
        storage = self.prefix+'/providers/Microsoft.Storage/storageAccounts/pwstate'
        before = {
            'azurerm_user_assigned_identity': {'id': identity, 'resource_group_name': 'rg-pathwatch-lab',
                'principal_id': CI_PRINCIPAL, 'tags': {'project': 'pathwatch', 'owner': 'codex-pathwatch'}},
            'azurerm_federated_identity_credential': {'id': identity+'/federatedIdentityCredentials/github-main', 'parent_id': identity},
            'azurerm_role_assignment': {'id': storage+'/providers/Microsoft.Authorization/roleAssignments/'+SUB,
                'scope': storage, 'role_definition_name': 'Storage Blob Data Contributor', 'principal_id': CI_PRINCIPAL},
        }
        self.plan = {'variables': {'subscription_id': {'value': SUB}, 'state_storage_account_id': {'value': storage}},
            'resource_changes': [{'mode': 'managed', 'address': address, 'type': typ,
                'change': {'actions': ['delete'], 'before': before[typ]}} for address,typ in teardown.ADDRESSES.items()]}

    def test_valid_identity_destroy_scope(self):
        teardown.validate(self.plan, SUB)
        empty = copy.deepcopy(self.plan);empty['resource_changes'] = []
        teardown.validate(empty, SUB)

    def test_principal_uuid_case_canonicalization_and_destroy_layout(self):
        # Actual Terraform global destroy shape: known UAMI/role principals live in
        # change.before, after is null; role may precede identity in the JSON list.
        plan = copy.deepcopy(self.plan)
        plan['resource_changes'][0]['change']['before']['principal_id'] = CI_PRINCIPAL.upper()
        for resource in plan['resource_changes']:
            resource['change']['after'] = None
            resource['change']['after_unknown'] = {}
        plan['resource_changes'].reverse()
        teardown.validate(plan, SUB)

    def test_unrelated_principal_refused(self):
        plan = copy.deepcopy(self.plan)
        plan['resource_changes'][2]['change']['before']['principal_id'] = '44444444-4444-4444-4444-444444444444'
        with self.assertRaisesRegex(ValueError, 'principal'):
            teardown.validate(plan, SUB)

    def test_missing_empty_or_invalid_principals_refused(self):
        for index in (0, 2):
            for value in (None, '', 'not-a-uuid', '00000000-0000-0000-0000-000000000000'):
                plan = copy.deepcopy(self.plan)
                if value is None:
                    del plan['resource_changes'][index]['change']['before']['principal_id']
                else:
                    plan['resource_changes'][index]['change']['before']['principal_id'] = value
                with self.subTest(index=index, value=value), self.assertRaisesRegex(ValueError, 'principal'):
                    teardown.validate(plan, SUB)

    def test_orphan_role_without_owned_identity_before_state_refused(self):
        plan = copy.deepcopy(self.plan)
        plan['resource_changes'] = [plan['resource_changes'][2]]
        with self.assertRaisesRegex(ValueError, 'reconcile|import'):
            teardown.validate(plan, SUB)

    def test_no_backend_rg_or_unrecognized_resources(self):
        for typ in ('azurerm_resource_group', 'azurerm_storage_account', 'azurerm_storage_container'):
            plan = copy.deepcopy(self.plan)
            plan['resource_changes'][0]['type'] = typ
            with self.assertRaises(ValueError):teardown.validate(plan, SUB)

    def test_unsafe_scope_and_ownership(self):
        for index,key,value in ((0,'id',self.prefix.replace('rg-pathwatch-lab','rg-mlinfra-westus')),
                                (0,'tags',{}), (1,'parent_id',self.prefix+'/providers/Other/identity/other'),
                                (2,'scope','/subscriptions/'+SUB+'/resourceGroups/rg-mlinfra-westus'),
                                (2,'role_definition_name','Owner')):
            plan = copy.deepcopy(self.plan)
            plan['resource_changes'][index]['change']['before'][key] = value
            with self.assertRaises(ValueError):teardown.validate(plan, SUB)
        with self.assertRaises(ValueError):teardown.validate(self.plan, '22222222-2222-2222-2222-222222222222')

    def test_create_update_and_replace_refused(self):
        for actions in (['create'], ['update'], ['delete','create']):
            plan = copy.deepcopy(self.plan);plan['resource_changes'][0]['change']['actions'] = actions
            with self.assertRaises(ValueError):teardown.validate(plan, SUB)

    def test_cli_binds_reviewed_binary_digest(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as td:
            td = Path(td);plan = td/'destroy.tfplan';plan.write_bytes(b'local fake saved plan')
            fake = td/'terraform';fake.write_text('#!/usr/bin/env python3\nprint('+repr(json.dumps(self.plan))+')\n');fake.chmod(0o755)
            env = os.environ | {'PATH': str(td)+':'+os.environ['PATH'], 'AZURE_SUBSCRIPTION_ID': SUB}
            command = [sys.executable, str(ROOT/'scripts/azure_teardown.py'), '--plan', str(plan), '--reviewed-sha256']
            refused = subprocess.run(command+['0'*64], env=env, text=True, capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            accepted = subprocess.run(command+[hashlib.sha256(plan.read_bytes()).hexdigest()], env=env, text=True, capture_output=True)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_root_down_routes_only_selected_environment(self):
        result = subprocess.run(['make','-n','down','ENV=infra/envs/dev/global'], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('azure_teardown.py', result.stdout)
        self.assertIn('infra/envs/dev/global', result.stdout)
        self.assertNotIn('azure_reaper.sh', result.stdout)
        result = subprocess.run(['make','down','ENV=infra/bootstrap'], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('retained state', result.stderr)
        self.assertNotIn('azure_reaper.sh', result.stdout)
        result = subprocess.run(['make','down','ENV=infra/other'], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        result = subprocess.run(['make','-n','azure-down'], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('azure_reaper.sh down', result.stdout)

if __name__ == '__main__':
    unittest.main()
