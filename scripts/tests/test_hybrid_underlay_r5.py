"""R5 primary underlay loss and explicit FRR GR policy, without live execution."""
import copy
import json
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hybrid_runtime import Runtime, graceful_restart_disabled
from hybrid_config import frr_config, SITES


class Firewall:
    def __init__(self):
        self.rules = []
        self.calls = []
        self.failure = None

    def run(self, node, args):
        assert node == 'home'
        assert args[:3] == ['iptables', '-w', '5']
        op, chain = args[3:5]
        rule = (chain, tuple(args[6:] if op == '-I' else args[5:]))
        self.calls.append((op, chain, rule))
        failure = self.failure
        if failure and failure[:2] == (op, chain):
            self.failure = None
            if failure[2]: self.rules.append(rule)  # Insert succeeded, reply was lost.
            raise failure[3]
        if op == '-C':
            if rule not in self.rules: raise subprocess.CalledProcessError(1, args)
        elif op == '-I': self.rules.insert(0, rule)
        elif op == '-D': self.rules.remove(rule)
        else: raise AssertionError('Only exact check/insert/delete rules allowed')
        return ''

    def blocked(self, chain, src, dst, sport, dport):
        for actual_chain, rule in self.rules:
            if actual_chain != chain: continue
            fields = dict(zip(rule[::2], rule[1::2]))
            if (fields.get('-s', src) == src and fields.get('-d', dst) == dst
                    and fields.get('--sport', sport) == sport and fields.get('--dport', dport) == dport):
                return True
        return False


class UnderlayTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime.__new__(Runtime)
        self.runtime.config = {'nva_public_ip': '8.8.8.8'}
        self.firewall = Firewall()
        self.runtime.run = self.firewall.run

    def test_both_directions_only_primary_owned_endpoint_and_ports(self):
        self.runtime.impair(True)
        self.assertEqual(len(self.firewall.rules), 2)
        self.assertTrue(self.firewall.blocked('OUTPUT', '10.30.0.2', '8.8.8.8', '51820', '51820'))
        self.assertTrue(self.firewall.blocked('INPUT', '8.8.8.8', '10.30.0.2', '51820', '51820'))
        for chain, src, dst in [('OUTPUT', '10.30.0.2', '8.8.8.8'), ('INPUT', '8.8.8.8', '10.30.0.2')]:
            for sport, dport in [('51821', '51821'), ('51820', '51821'), ('51821', '51820')]:
                with self.subTest(chain=chain, sport=sport, dport=dport):
                    self.assertFalse(self.firewall.blocked(chain, src, dst, sport, dport))
        self.assertFalse(self.firewall.blocked('OUTPUT', '10.30.0.2', '1.1.1.1', '51820', '51820'))
        self.assertFalse(self.firewall.blocked('INPUT', '1.1.1.1', '10.30.0.2', '51820', '51820'))
        for _, rule in self.firewall.rules:
            self.assertIn('pathwatch-task3-underlay', rule)
            self.assertIn('udp', rule)
            self.assertEqual(rule[-2:], ('-j', 'DROP'))

    def test_enable_disable_idempotent_and_leaves_foreign_rule(self):
        foreign = ('OUTPUT', ('-d', '1.1.1.1', '-p', 'udp', '--dport', '51820', '-j', 'DROP'))
        self.firewall.rules.append(foreign)
        self.runtime.impair(True); self.runtime.impair(True)
        self.assertEqual(len(self.firewall.rules), 3)
        self.runtime.impair(False); self.runtime.impair(False)
        self.assertEqual(self.firewall.rules, [foreign])

    def test_partial_insert_failure_or_cancel_rolls_back_both_directions(self):
        for chain in ('OUTPUT', 'INPUT'):
            for committed in (False, True):
                for error in (subprocess.CalledProcessError(4, 'insert'), subprocess.TimeoutExpired('insert', 20), KeyboardInterrupt()):
                    self.firewall.rules.clear()
                    self.firewall.failure = ('-I', chain, committed, error)
                    with self.subTest(chain=chain, committed=committed, error=type(error).__name__):
                        with self.assertRaises(type(error)): self.runtime.impair(True)
                        self.assertEqual(self.firewall.rules, [])

    def test_failed_delete_still_attempts_other_direction_and_reports_failure(self):
        for chain in ('OUTPUT', 'INPUT'):
            for error in (subprocess.CalledProcessError(1, 'delete'), subprocess.CalledProcessError(4, 'delete'), subprocess.TimeoutExpired('delete', 20)):
                self.runtime.impair(True)
                self.firewall.failure = ('-D', chain, False, error)
                with self.subTest(chain=chain, error=type(error).__name__):
                    with self.assertRaises(type(error)): self.runtime.impair(False)
                    self.assertEqual([r[0] for r in self.firewall.rules], [chain])
                    self.runtime.impair(False)
                    self.assertEqual(self.firewall.rules, [])

    def test_check_failure_does_not_skip_other_direction_cleanup(self):
        self.runtime.impair(True)
        self.firewall.failure = ('-C', 'OUTPUT', False, subprocess.CalledProcessError(4, 'check'))
        with self.assertRaises(subprocess.CalledProcessError): self.runtime.impair(False)
        self.assertEqual([r[0] for r in self.firewall.rules], ['OUTPUT'])
        self.runtime.impair(False)

    def test_actual_command_targets_only_labeled_home_router_namespace(self):
        args = ['iptables', '-w', '5', '-C', 'INPUT']
        with patch('hybrid_runtime.subprocess.check_output', return_value='[{"Config":{"Labels":{"project":"pathwatch"}}}]'):
            self.assertEqual(self.runtime.command('home', args), ['docker', 'exec', '-i', 'pathwatch-hybrid-home-router'] + args)
        with patch('hybrid_runtime.subprocess.check_output', return_value='[{"Config":{"Labels":{"project":"foreign"}}}]'):
            with self.assertRaises(ValueError): self.runtime.command('home', args)


class GracefulRestartTests(unittest.TestCase):
    @staticmethod
    def neighbors(node):
        # FRR 8.4.4 show bgp neighbors json uses these exact field names.
        # The configured mode is available before session establishment.
        return {peer: {'bgpState': 'Idle', 'gracefulRestartInfo': {'localGrMode': mode}}
                for peer, mode in zip(SITES[node]['peers'], ('Disable*', 'Disable'))}

    def profile_reply(self, node, args):
        if args[-1] == 'show bgp neighbors json':
            return json.dumps(self.neighbors(node))
        return ''  # Parse success alone does not establish GR semantics.

    def test_captured_actual_daemon_modes(self):
        captures = json.loads((Path(__file__).parent / 'fixtures/frr844-gr-modes.json').read_text())
        for name, capture in captures.items():
            for site, neighbors in capture['sites'].items():
                with self.subTest(capture=name, site=site):
                    self.assertEqual(graceful_restart_disabled(neighbors, SITES[site]['peers']), name == 'disabled')

    def test_all_six_rendered_configs_explicitly_disable_gr_and_helper(self):
        for site in SITES:
            for profile in ('baseline', 'tuned', 'bfd'):
                with self.subTest(site=site, profile=profile):
                    config = frr_config(site, profile)
                    self.assertIn('\n bgp graceful-restart-disable\n', config)
                    self.assertNotIn('no bgp graceful-restart', config)
                    self.assertNotIn('bgp graceful-restart disable', config)
                    self.assertEqual(config.count('maximum-prefix 1'), 2)
                    self.assertIn('set local-preference 200', config)
                    self.assertIn('set local-preference 100', config)

    def test_live_profile_disables_gr_on_both_sites_before_session_reset(self):
        runtime = Runtime.__new__(Runtime)
        queries = {}
        def reply(node, args):
            if args[-1] == 'show bgp neighbors json':
                queries[node] = queries.get(node, 0) + 1
                neighbors = self.neighbors(node)
                if queries[node] == 1:
                    for peer in neighbors.values(): peer['gracefulRestartInfo']['localGrMode'] = 'Helper*'
                return json.dumps(neighbors)
            return ''
        runtime.run = MagicMock(side_effect=reply)
        for profile in ('baseline', 'tuned', 'bfd'):
            queries.clear(); runtime.run.reset_mock(); runtime.profile(profile)
            self.assertEqual(runtime.run.call_count, 6)
            for before, call, verification in zip(runtime.run.call_args_list[::3], runtime.run.call_args_list[1::3], runtime.run.call_args_list[2::3]):
                node, args = call.args
                sudo = ['sudo'] if node == 'azure' else []
                self.assertEqual(before.args, (node, sudo + ['vtysh', '-c', 'show bgp neighbors json']))
                self.assertEqual(verification.args, before.args)
                commands = [args[i+1] for i, value in enumerate(args) if value == '-c']
                with self.subTest(node=node, profile=profile):
                    self.assertIn('bgp graceful-restart-disable', commands)
                    self.assertLess(commands.index('bgp graceful-restart-disable'), commands.index('clear bgp *'))
                    for peer in SITES[node]['peers']:
                        self.assertIn('neighbor '+peer+' timers '+('3 9' if profile == 'tuned' else '60 180'), commands)
                    self.assertEqual(sum(c.endswith('bfd profile PATHWATCH') for c in commands), 2 if profile == 'bfd' else 0)

    def test_profile_rejects_actual_helper_unknown_or_malformed_mode_on_either_site(self):
        for node in ('home', 'azure'):
            good = self.neighbors(node)
            peer = SITES[node]['peers'][0]
            invalid = [None, [], {}, {peer: good[peer]},
                       {**good, '10.255.0.99': good[peer]},
                       {**{k: v for k, v in good.items() if k != peer}, '10.255.0.99': good[peer]}]
            for value in (None, [], {}, {'gracefulRestartInfo': None},
                          {'gracefulRestartInfo': []}, {'gracefulRestartInfo': {}},
                          *({'gracefulRestartInfo': {'localGrMode': mode}} for mode in
                            ('Helper*', 'Helper', 'Restart', 'Disabled', '', None, [], {}))):
                changed = copy.deepcopy(good); changed[peer] = value; invalid.append(changed)
            for neighbors in invalid:
                runtime = Runtime.__new__(Runtime)
                queries = []
                def reply(site, args):
                    if site == node and args[-1] == 'show bgp neighbors json':
                        queries.append(site)
                        if len(queries) == 2: return json.dumps(neighbors)
                    return self.profile_reply(site, args)
                runtime.run = MagicMock(side_effect=reply)
                with self.subTest(node=node, neighbors=neighbors):
                    with self.assertRaisesRegex(RuntimeError, node + '.*GR/helper'):
                        runtime.profile('baseline')

    def test_profile_does_not_accept_failed_or_non_json_actual_verification(self):
        for query_number in (1, 2):  # Fail either precheck or mandatory postcheck.
            for failure in ('% Unknown command', '', subprocess.CalledProcessError(1, 'vtysh'),
                            subprocess.TimeoutExpired('vtysh', 20)):
                runtime = Runtime.__new__(Runtime)
                queries = []
                def reply(node, args):
                    if args[-1] == 'show bgp neighbors json':
                        queries.append(node)
                        if len(queries) == query_number:
                            if isinstance(failure, Exception): raise failure
                            return failure
                    return self.profile_reply(node, args)
                runtime.run = MagicMock(side_effect=reply)
                expected = type(failure) if isinstance(failure, Exception) else json.JSONDecodeError
                with self.subTest(query=query_number, failure=failure), self.assertRaises(expected):
                    runtime.profile('baseline')

    def test_repeated_disabled_profile_skips_only_global_disable(self):
        runtime = Runtime.__new__(Runtime)
        def reply(node, args):
            if 'bgp graceful-restart-disable' in args:
                raise subprocess.CalledProcessError(1, args, output='% The Graceful Restart command used is not valid at this moment.')
            return self.profile_reply(node, args)
        runtime.run = MagicMock(side_effect=reply)
        for profile in ('baseline', 'tuned', 'bfd'):
            for repeat in range(2):
                with self.subTest(profile=profile, repeat=repeat):
                    runtime.run.reset_mock(); runtime.profile(profile)
                    self.assertEqual(runtime.run.call_count, 6)
                    for before, config, after in zip(runtime.run.call_args_list[::3], runtime.run.call_args_list[1::3], runtime.run.call_args_list[2::3]):
                        node, args = config.args
                        self.assertEqual(before.args, after.args)
                        self.assertEqual(before.args[1][-1], 'show bgp neighbors json')
                        self.assertNotIn('bgp graceful-restart-disable', args)
                        self.assertIn('clear bgp *', args)
                        for peer in SITES[node]['peers']:
                            self.assertIn('no neighbor '+peer+' bfd', args)
                            self.assertIn('neighbor '+peer+' timers '+('3 9' if profile == 'tuned' else '60 180'), args)
                        self.assertEqual(sum(c.endswith('bfd profile PATHWATCH') for c in args), 2 if profile == 'bfd' else 0)

    def test_invalid_precheck_refuses_configuration_on_that_site(self):
        for node in ('home', 'azure'):
            peer = SITES[node]['peers'][0]
            for invalid in (None, [], {}, {peer: {}},
                            {**self.neighbors(node), '10.255.0.99': {}},
                            {**self.neighbors(node), peer: {'gracefulRestartInfo': {'localGrMode': 'Invalid*'}}}):
                runtime = Runtime.__new__(Runtime)
                def reply(site, args):
                    if site == node and args[-1] == 'show bgp neighbors json': return json.dumps(invalid)
                    return self.profile_reply(site, args)
                runtime.run = MagicMock(side_effect=reply)
                with self.subTest(node=node, invalid=invalid):
                    with self.assertRaisesRegex(RuntimeError, node + '.*GR/helper'):
                        runtime.profile('baseline')
                    self.assertFalse(any(call.args[0] == node and 'configure terminal' in call.args[1]
                                         for call in runtime.run.call_args_list))

    def test_other_configuration_errors_are_never_ignored(self):
        for node in ('home', 'azure'):
            for mode in ('Disable*', 'Helper*'):
                for error in (subprocess.CalledProcessError(1, 'vtysh', output='% Unknown command'),
                              subprocess.TimeoutExpired('vtysh', 20)):
                    runtime = Runtime.__new__(Runtime)
                    def reply(site, args):
                        if site == node:
                            if 'configure terminal' in args: raise error
                            neighbors = self.neighbors(site)
                            for peer in neighbors.values(): peer['gracefulRestartInfo']['localGrMode'] = mode
                            return json.dumps(neighbors)
                        return self.profile_reply(site, args)
                    runtime.run = MagicMock(side_effect=reply)
                    with self.subTest(node=node, mode=mode, error=error):
                        with self.assertRaises(type(error)) as caught: runtime.profile('baseline')
                        self.assertIs(caught.exception, error)
                        self.assertIn('configure terminal', runtime.run.call_args.args[1])
