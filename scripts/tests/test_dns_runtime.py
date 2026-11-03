import ast
import datetime as dt
import json
import shlex
import signal
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dns_config import corefile, wireguard
from dns_failover import analyze, campaign
from dns_runtime import Runtime, RateLimit
from dns_monitor import enforce

class DNSRuntimeTests(unittest.TestCase):
    def test_forward_only_owned_zone_and_bind_private(self):
        for site,ip in [('a','10.10.0.4'),('b','10.20.0.4')]:
            config=corefile(site)
            self.assertIn('dns.pathwatch.internal:53',config)
            self.assertIn('bind '+ip,config)
            self.assertIn('forward . 168.63.129.16',config)
            self.assertNotIn('.:53',config)
            self.assertNotIn('0.0.0.0',config)
    def test_public_interval_and_targets_enforced(self):
        clock=MagicMock(return_value=10);rate=RateLimit(clock)
        rate.take('home','dns')
        with self.assertRaises(ValueError):rate.take('home','dns')
        clock.return_value=70;rate.take('home','dns')
        with self.assertRaises(ValueError):rate.take('home','dns')
    def test_usage_absolute_boot_bounds_80_percent(self):
        def sample(ops,tx):return {'disks':{'sda':{'bounded_transactions':ops}},'tx_bytes':{'eth0':tx}}
        enforce(sample(799999,199999999))
        for value in [sample(800000,0),sample(0,200000000)]:
            with self.assertRaises(ValueError):enforce(value)
    def test_window_requires_three_origins_and_real_backup_only(self):
        rows=[]
        for site in ('home','a','b'):
            rows += [{'site':site,'monotonic_ns':1000000000,'answers':['8.8.8.8']},{'site':site,'monotonic_ns':61000000000,'answers':['1.1.1.1']}]
        result=analyze(rows,'8.8.8.8','1.1.1.1')
        self.assertEqual(set(result),{'home','a','b'})
        with self.assertRaises(ValueError):analyze(rows[:-1],'8.8.8.8','1.1.1.1')
        rows[-1]['answers']=['8.8.8.8','1.1.1.1']
        with self.assertRaises(ValueError):analyze(rows,'8.8.8.8','1.1.1.1')
    def test_campaign_failure_restores_and_never_writes_summary(self):
        runtime=MagicMock();runtime.config={'sites':{'a':{'public_ip':'8.8.8.8'},'b':{'public_ip':'1.1.1.1'}}}
        runtime.observe.return_value={'site':'home','monotonic_ns':1,'answers':['8.8.8.8']}
        runtime.service.side_effect=[RuntimeError('stop reply lost'),None]
        with tempfile.TemporaryDirectory() as out:
            with patch('dns_failover.Monitor'),self.assertRaises(RuntimeError):campaign(runtime,out)
            self.assertEqual(runtime.service.call_args_list[-1].args,('start',))
            run=next(Path(out).iterdir());self.assertFalse((run/'summary.json').exists())
            self.assertEqual(json.loads((run/'run.json').read_text())['status'],'failed')
    def test_restore_failure_does_not_publish_pass(self):
        runtime=MagicMock();runtime.config={'sites':{'a':{'public_ip':'8.8.8.8'},'b':{'public_ip':'1.1.1.1'}}}
        runtime.service.side_effect=[None,RuntimeError('restore failed')]
        rows=[{'site':s,'monotonic_ns':1,'answers':['8.8.8.8']} for s in ('home','a','b')]+[{'site':s,'monotonic_ns':61000000001,'answers':['1.1.1.1']} for s in ('home','a','b')]
        runtime.observe.side_effect=rows
        with tempfile.TemporaryDirectory() as out:
            with patch('dns_failover.Monitor'),patch('dns_failover.time.sleep'),self.assertRaisesRegex(RuntimeError,'restore'):campaign(runtime,out,rounds=1)
            run=next(Path(out).iterdir());self.assertFalse((run/'summary.json').exists())

    def test_durable_public_rate_across_new_runtime_and_clock_reversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rate.json'
            with patch('dns_runtime.time.time',return_value=100):RateLimit(path=path,campaign='c').take('home','target')
            for now in (99,159):
                with patch('dns_runtime.time.time',return_value=now),self.assertRaises(ValueError):RateLimit(path=path,campaign='c').take('home','target')
            with patch('dns_runtime.time.time',return_value=160):RateLimit(path=path,campaign='c').take('home','target')

    def test_proposal_controller_and_guest_usage_bounds_agree(self):
        from dns_proposal import quantities
        disk=quantities('azapi_update_resource','2026-10-24T06:00:00Z','2026-10-24T08:00:00Z')['transactions']
        egress=quantities('azurerm_linux_virtual_machine','2026-10-24T06:00:00Z','2026-10-24T08:00:00Z')['egress_gb']
        with patch('dns_monitor.usage_enforce') as guard:
            enforce({});self.assertEqual(guard.call_args.kwargs,{'egress_gb':float(egress),'transactions':int(disk)})
        runtime=Runtime.__new__(Runtime);runtime.root=Path(__file__).resolve().parents[2];runtime.key=Path('/fake/key');runtime.known=Path('/fake/known')
        runtime.config={'ttl':'2100-01-01T00:00:00Z','sites':{'a':{'public_ip':'8.8.8.8'}}}
        with patch('dns_runtime.subprocess.run',return_value=MagicMock(stdout='')) as run:
            runtime.run('a',['sleep','1'])
        argv=run.call_args.args[0];self.assertIn('/dev/null',argv);self.assertIn('IdentitiesOnly=yes',argv)
        code=shlex.split(argv[-1])[-1];tree=ast.parse(code)
        call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Subscript) and isinstance(n.func.slice,ast.Constant) and n.func.slice.value=='enforce')
        self.assertEqual({k.arg:ast.literal_eval(k.value) for k in call.keywords},{'egress_gb':float(egress),'transactions':int(disk)})

    def test_guest_watchdog_kills_child_group_after_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'scripts').mkdir()
            (root/'scripts/hybrid_usage.py').write_text("count=0\ndef snapshot():\n global count\n count+=1\n return 0 if count==1 else 1000000\ndef enforce(sample,egress_gb,transactions):\n if sample>=transactions*.8:raise ValueError('80% stop')\n")
            runtime=Runtime.__new__(Runtime);runtime.root=root;runtime.key=Path('/fake/key');runtime.known=Path('/fake/known');runtime.config={'ttl':'2100-01-01T00:00:00Z','sites':{'a':{'public_ip':'8.8.8.8'}}}
            with patch('dns_runtime.subprocess.run',return_value=MagicMock(stdout='')) as run:runtime.run('a',['sleep','1'])
            code=shlex.split(run.call_args.args[0][-1])[-1]
            process=MagicMock(pid=123);process.poll.return_value=None
            previous={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP)}
            try:
                with patch('subprocess.Popen',return_value=process),patch('os.killpg') as kill,patch('time.sleep'),self.assertRaisesRegex(ValueError,'80%'):
                    exec(code,{})
                self.assertEqual([c.args for c in kill.call_args_list],[(123,signal.SIGTERM),(123,signal.SIGKILL)])
            finally:
                for sig,handler in previous.items():signal.signal(sig,handler)
