"""Watchdog state transitions with real usage math; external SSH/deletion are boundaries."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import datetime as dt
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))

class WatchTests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/probe_usage_watch.py';self.assertTrue(path.is_file(),'missing single probe actual-usage watchdog')
        spec=importlib.util.spec_from_file_location('watch',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.cache');self.addCleanup(self.tmp.cleanup);self.out=Path(self.tmp.name)/'terminal.json'
        self.now=dt.datetime(2026,10,24,20,tzinfo=dt.timezone.utc)
    def sample(self,ops=1,tx=1,boot='boot-one'):
        return {'boot_id':boot,'disks':{'sda':{'bounded_transactions':ops}},'tx_bytes':{'eth0':tx}}
    def run_watch(self,samples,cleanup=None):
        iterator=iter(samples);deleted=[]
        def read():
            value=next(iterator)
            if isinstance(value,Exception):raise value
            return value
        result=self.module.watch(read,cleanup or (lambda:deleted.append('exact saved plan deleted')),lambda:self.now,lambda _:None,1000000,2,self.now+dt.timedelta(hours=1),self.out)
        return result,deleted,json.loads(self.out.read_text())
    def test_80_percent_control_traffic_stops_and_preserves_last_sample(self):
        result,deleted,record=self.run_watch([self.sample(),self.sample(ops=10,tx=1600000000)])
        self.assertEqual(result,'usage_limit');self.assertEqual(len(deleted),1)
        self.assertEqual(record['last_sample']['tx_bytes']['eth0'],1600000000)
        self.assertTrue(record['deleted'])
    def test_missing_sample_is_not_zero_and_triggers_exact_cleanup(self):
        result,deleted,record=self.run_watch([self.sample(ops=50),RuntimeError('SSH failed')])
        self.assertEqual(result,'sampling_failure');self.assertEqual(len(deleted),1)
        self.assertEqual(record['last_sample']['disks']['sda']['bounded_transactions'],50)
        self.assertIn('SSH failed',record['error'])
    def test_reboot_and_counter_decrease_cannot_reset_bounds(self):
        for second in (self.sample(boot='boot-two'),self.sample(ops=1)):
            result,deleted,record=self.run_watch([self.sample(ops=100),second])
            self.assertEqual(result,'sampling_failure');self.assertEqual(len(deleted),1)
    def test_cleanup_failure_is_visible_and_retains_terminal_sample(self):
        def cleanup():raise RuntimeError('terraform delete failed')
        with self.assertRaisesRegex(RuntimeError,'terraform delete failed'):self.run_watch([self.sample(ops=800000)],cleanup)
        record=json.loads(self.out.read_text());self.assertFalse(record['deleted']);self.assertIn('terraform delete failed',record['cleanup_error'])
    def test_evidence_write_failure_does_not_prevent_cleanup(self):
        self.out.mkdir();deleted=[]
        with self.assertRaises(OSError):self.run_watch([self.sample(ops=800000)],lambda:deleted.append('deleted'))
        self.assertEqual(deleted,['deleted'])
if __name__=='__main__':unittest.main()
