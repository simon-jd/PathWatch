"""CLI regressions for serialized ledger admission and no-op plans; all cloud/tool IO is fake."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SUB = '11111111-1111-1111-1111-111111111111'


class JournalCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.ledger = self.directory / 'ledger.json'
        self.ledger.write_text('{"schema":1,"resources":[]}\n')
        fake = self.directory / 'terraform'
        fake.write_text('''#!/usr/bin/env python3
import pathlib,sys
print(pathlib.Path(sys.argv[-1]+'.json').read_text())
''')
        fake.chmod(0o755)
        # Each subprocess executes the real parser/main/render/admission/persistence path.
        # Only canonical ledger location and external terraform are replaced.
        self.worker = self.directory / 'worker.py'
        self.worker.write_text('''import importlib.util,os,pathlib,sys
spec=importlib.util.spec_from_file_location('budget',sys.argv.pop(1))
budget=importlib.util.module_from_spec(spec);spec.loader.exec_module(budget)
budget.LEDGER=pathlib.Path(sys.argv.pop(1))
if os.environ.get('SIGNAL_FLOCK') and hasattr(budget,'fcntl'):
    original=budget.fcntl.flock
    def signal(fd,operation):
        if operation == budget.fcntl.LOCK_EX:
            (budget.LEDGER.parent/('attempt-'+str(os.getpid()))).touch()
        return original(fd,operation)
    budget.fcntl.flock=signal
try:budget.main()
except (ValueError,KeyError,TypeError,OSError) as e:
    print('BUDGET REFUSED:',e,file=sys.stderr);sys.exit(1)
''')
        self.env = os.environ | {'AZURE_SUBSCRIPTION_ID': SUB, 'PATH': str(self.directory)+':'+os.environ['PATH']}

    def fixture(self, name, noop=False, amount=10):
        now = dt.datetime.now(dt.timezone.utc)
        start = (now-dt.timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        end = (now+dt.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        address = 'azurerm_resource_group.'+name
        after = {'name': 'rg-pathwatch-lab', 'location': 'westus2', 'tags': {
            'project': 'pathwatch', 'owner': 'codex-pathwatch', 'ttl': end, 'est_usd_per_hour': '0'}}
        plan = {'variables': {'subscription_id': {'value': SUB}, 'location': {'value': 'westus2'}},
                'resource_changes': [{'mode': 'managed', 'address': address, 'type': 'azurerm_resource_group',
                                      'change': {'actions': ['no-op'] if noop else ['create'], 'after': after}}]}
        plan_path = self.directory / (name+'.tfplan')
        plan_path.write_bytes(('distinct binary plan '+name).encode())
        Path(str(plan_path)+'.json').write_text(json.dumps(plan))
        row = {'address': address, 'type': 'azurerm_resource_group', 'region': 'westus2',
               'start_utc': start, 'end_utc': end, 'estimate_usd': amount, 'meters': []}
        proposal = self.directory / (name+'.proposal.json')
        proposal.write_text(json.dumps({'resources': [] if noop else [row]}))
        return plan_path, proposal

    def command(self, operation, fixture):
        return [sys.executable, str(self.worker), str(ROOT/'scripts/azure_budget.py'), str(self.ledger),
                operation, '--plan', str(fixture[0]), '--proposal', str(fixture[1])]

    def run_cli(self, operation, fixture):
        return subprocess.run(self.command(operation, fixture), env=self.env, text=True,
                              capture_output=True, timeout=10)

    def test_noop_reserve_then_check_preserves_ledger(self):
        fixture = self.fixture('unchanged', noop=True)
        before = self.ledger.read_bytes()
        for operation in ('reserve', 'check'):
            result = self.run_cli(operation, fixture)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ledger.read_bytes(), before)

    def assert_concurrent(self, amount, expected_successes):
        fixtures = [self.fixture(name, amount=amount) for name in ('first', 'second')]
        processes = []
        # Holding the same lock externally gives a deterministic rendezvous: both real
        # CLI processes reach flock, yet neither can read/admit/write a stale snapshot.
        with Path(str(self.ledger)+'.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                for fixture in fixtures:
                    processes.append(subprocess.Popen(self.command('reserve', fixture),
                        env=self.env | {'SIGNAL_FLOCK': '1'}, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                deadline = time.monotonic()+3
                while len(list(self.directory.glob('attempt-*'))) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(list(self.directory.glob('attempt-*'))), 2,
                                 'Both CLI admissions must reach the shared interprocess lock')
                self.assertTrue(all(p.poll() is None for p in processes))
                self.assertEqual(json.loads(self.ledger.read_text())['resources'], [])
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                outputs = [p.communicate(timeout=10) for p in processes]
        self.assertEqual(sum(p.returncode == 0 for p in processes), expected_successes, outputs)
        rows = json.loads(self.ledger.read_text())['resources']
        self.assertEqual(len(rows), expected_successes)
        self.assertEqual(sum(r['estimate_usd'] for r in rows), amount*expected_successes)
        self.assertEqual(len({r['plan_sha256'] for r in rows}), expected_successes)
        for process, fixture, output in zip(processes, fixtures, outputs):
            if process.returncode == 0:
                self.assertEqual(self.run_cli('check', fixture).returncode, 0)
            else:
                self.assertIn('18 USD', output[1])
                self.assertNotEqual(self.run_cli('check', fixture).returncode, 0)
        self.assertEqual(list(self.directory.glob('.ledger.json.*.tmp')), [])

    def test_concurrent_ten_dollar_reservations_obey_one_canonical_cap(self):
        self.assert_concurrent(10, 1)

    def test_concurrent_affordable_reservations_both_survive(self):
        self.assert_concurrent(4, 2)

if __name__ == '__main__':
    unittest.main()
