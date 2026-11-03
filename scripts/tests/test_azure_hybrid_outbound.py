"""R4 exact home-source forwarding rules; synthetic plans, no Azure calls."""
import copy
import unittest
import test_azure_nva_controls as nvatests
from test_azure_nva_controls import budget, teardown, SUB



class HybridOutboundTests(unittest.TestCase):
    def setUp(self):
        self.base = nvatests.NvaControlsTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.plan, self.proposal = self.base.plan, self.base.proposal
        # Reproduce the repair plan: two creates, all existing managed resources no-op.
        for r in self.plan['resource_changes']:
            if r['mode'] == 'managed' and '.hybrid_outbound[' not in r['address']:
                r['change']['actions'] = ['no-op']
                r['change']['before'] = copy.deepcopy(r['change']['after'])
        self.proposal['resources'] = [r for r in self.proposal['resources'] if '.hybrid_outbound[' in r['address']]

    def check(self):
        return budget.check(self.plan, self.proposal, {'resources': [{'estimate_usd': '.90'}]}, self.base.now)

    def test_exact_two_zero_cost_creates_with_existing_resources_noop(self):
        self.assertEqual(len(self.proposal['resources']), 2)
        self.check()
        self.assertTrue(all(r['estimate_usd'] == 0 for r in self.proposal['resources']))

    def test_outbound_mutations_fail_closed(self):
        original = copy.deepcopy(self.plan)
        for key in ('icmp', 'iperf'):
            for field, value in [('name', 'allow-other'), ('source_address_prefix', '*'),
                                 ('destination_address_prefix', '10.10.0.0/16'), ('protocol', 'Udp'),
                                 ('source_port_range', '123'), ('destination_port_range', '443'),
                                 ('direction', 'Inbound'), ('priority', 2602), ('access', 'Deny'),
                                 ('source_address_prefixes', ['0.0.0.0/0']),
                                 ('destination_address_prefixes', ['0.0.0.0/0']),
                                 ('source_port_ranges', ['*']), ('destination_port_ranges', ['*']),
                                 ('source_application_security_group_ids', ['foreign']),
                                 ('destination_application_security_group_ids', ['foreign']),
                                 ('network_security_group_name', 'nsg-foreign'),
                                 ('resource_group_name', 'rg-mlinfra-westus')]:
                self.plan = copy.deepcopy(original)
                rule = next(r for r in self.plan['resource_changes'] if f'.hybrid_outbound["{key}"]' in r['address'])
                rule['change']['after'][field] = value
                with self.subTest(key=key, field=field), self.assertRaises(ValueError):
                    self.check()
        self.plan = original

    def test_missing_outbound_rule_rejected_even_if_ledger_row_removed(self):
        self.plan['resource_changes'] = [r for r in self.plan['resource_changes'] if '.hybrid_outbound["icmp"]' not in r['address']]
        self.proposal['resources'] = [r for r in self.proposal['resources'] if '.hybrid_outbound["icmp"]' not in r['address']]
        with self.assertRaises(ValueError): self.check()

    def test_no_generic_update_replacement_type_or_name_admission(self):
        original = copy.deepcopy(self.plan)
        for field, value in [('actions', ['update']), ('actions', ['delete', 'create']),
                             ('type', 'azurerm_network_interface'), ('address', 'module.network.azurerm_network_security_rule.other["icmp"]')]:
            self.plan = copy.deepcopy(original)
            rule = next(r for r in self.plan['resource_changes'] if '.hybrid_outbound[' in r['address'])
            (rule['change'] if field == 'actions' else rule)[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): self.check()
        self.plan = original

    def test_parent_ownership_and_ttl_still_required(self):
        original = copy.deepcopy(self.plan)
        for field, value in [('owner', 'codex-warden'), ('ttl', '2000-01-01T00:00:00Z')]:
            self.plan = copy.deepcopy(original)
            parent = next(r for r in self.plan['resource_changes'] if r['mode'] == 'data')
            parent['change']['after']['tags'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.check()
        self.plan = original

    def test_owned_outbound_rules_delete_with_rest_of_owned_state(self):
        for r in self.plan['resource_changes']:
            if r['mode'] == 'managed':
                r['change'] = dict(actions=['delete'], before=r['change']['after'], after=None)
        teardown.validate(self.plan, SUB)
        rule = next(r for r in self.plan['resource_changes'] if '.hybrid_outbound[' in r['address'])
        rule['change']['before']['id'] = rule['change']['before']['id'].replace('rg-pathwatch-lab', 'rg-mlinfra-westus')
        with self.assertRaises(ValueError): teardown.validate(self.plan, SUB)
