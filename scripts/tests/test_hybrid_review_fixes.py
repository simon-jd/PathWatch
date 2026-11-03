"""I1/I2 regressions: synthetic endpoints and real temporary evidence files only."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock,patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import vpn_failover as failover
import test_hybrid as baseline

def packet_rows(statuses):
    return [{'line':f'10.10.0.4 : [{i}], '+('84 bytes, 1.00 ms' if ok else 'timed out'),
             'monotonic_ns':1000000000+i*100000000,'realtime_ns':1700000000000000000+i*100000000}
            for i,ok in enumerate(statuses)]

class RecoveryTests(unittest.TestCase):
    def test_recovered_then_unrecovered_tail_is_rejected(self):
        rows=packet_rows([True,False,True,False,False])
        with self.assertRaisesRegex(RuntimeError,'unrecovered'):
            failover.recovery_before_restore(rows,1050000000,1150000000,1450000000)
    def test_only_pre_switch_or_post_restore_success_is_rejected(self):
        rows=packet_rows([True,False,True,True,True,True,True])
        for switch,cutoff in ((1500000000,1650000000),(1150000000,1150000000)):
            with self.subTest(switch=switch,cutoff=cutoff),self.assertRaises(RuntimeError):
                failover.recovery_before_restore(rows,1050000000,switch,cutoff)
    def test_stale_terminal_success_is_not_effective_current_traffic(self):
        with self.assertRaisesRegex(RuntimeError,'stale'):
            failover.recovery_before_restore(packet_rows([True,False,True,True,True,True,True]),1050000000,1250000000,2500000000)
    def test_effective_post_switch_success_under_impairment_is_accepted(self):
        rows=packet_rows([True,False,True,True,True,True,True])
        result=failover.recovery_before_restore(rows,1050000000,1250000000,1650000000)
        self.assertGreaterEqual(result['recovery_proof']['terminal_success_samples'],3)
        self.assertLessEqual(result['recovery_proof']['last_success_monotonic_ns'],1650000000)
    def test_trial_validates_failure_and_success_before_restore(self):
        # Real trial flow invokes the real validator on deterministic raw
        # fixtures; endpoint commands and process scheduling are synthetic.
        for statuses,reject in (([True,False,True,False,False],True),([True,False,True,True,True,True,True],False)):
            runtime=MagicMock();active={'value':False};runtime.impair.side_effect=lambda enabled:active.update(value=enabled)
            runtime.home_route.return_value=[{'dev':'pw-wg1'}];runtime.snapshot.return_value={}
            process=MagicMock();process.stdout=[];process.poll.return_value=None
            with tempfile.TemporaryDirectory() as out:
                drill=failover.Drill(runtime,out);drill.wait_ready=MagicMock(return_value={})
                real=getattr(failover,'recovery_before_restore',None)
                if real is None:self.fail('trial needs recovery_before_restore validation')
                def fixture_assess(*args):
                    self.assertTrue(active['value'],'validate traffic before restoring UDP')
                    return real(packet_rows(statuses),1050000000,1150000000 if reject else 1250000000,1450000000 if reject else 1650000000)
                expectation=self.assertRaisesRegex(RuntimeError,'unrecovered') if reject else contextlib.nullcontext()
                with patch.object(failover.subprocess,'Popen',return_value=process),patch.object(failover.time,'sleep'),patch.object(failover,'recovery_before_restore',side_effect=fixture_assess),expectation:
                    result=drill.trial('baseline',1)
                self.assertFalse(active['value']);runtime.stop_probe.assert_called_once()
                self.assertEqual((Path(out)/'baseline-1/result.json').exists(),not reject)
                if not reject:self.assertGreaterEqual(result['traffic']['recovery_proof']['terminal_success_samples'],3)

class EvidenceRerunTests(unittest.TestCase):
    def test_baseline_success_then_failure_uses_fresh_run_without_stale_pass(self):
        runtime=MagicMock();runtime.run.return_value='{}'
        with tempfile.TemporaryDirectory() as out,patch.object(baseline.Drill,'wait_ready',return_value={}),patch.object(baseline.Drill,'usage'),patch.object(baseline,'Monitor'),contextlib.redirect_stdout(io.StringIO()):
            first=baseline.run_baseline(runtime,out)
            success=(first/'acceptance.json').read_bytes()
            self.assertEqual(json.loads((first/'run.json').read_text())['status'],'passed')
            runtime.run.side_effect=RuntimeError('second run failed')
            with self.assertRaises(RuntimeError):baseline.run_baseline(runtime,out)
            runs=sorted(Path(out).glob('run-*'));self.assertEqual(len(runs),2)
            second=next(run for run in runs if run!=first)
            self.assertEqual((first/'acceptance.json').read_bytes(),success)
            self.assertFalse((second/'acceptance.json').exists())
            self.assertEqual(json.loads((second/'run.json').read_text())['status'],'failed')
    def test_cancelled_baseline_records_failed_run_without_acceptance(self):
        runtime=MagicMock();runtime.run.side_effect=KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as out,patch.object(baseline.Drill,'wait_ready',return_value={}),patch.object(baseline.Drill,'usage'),patch.object(baseline,'Monitor'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):baseline.run_baseline(runtime,out)
            run=next(Path(out).glob('run-*'))
            self.assertFalse((run/'acceptance.json').exists())
            self.assertEqual(json.loads((run/'run.json').read_text())['status'],'failed')
    def test_campaign_success_then_failure_has_no_stale_summary_or_mixed_raw(self):
        runtime=MagicMock()
        def good(drill,profile,index):
            trial=drill.out/f'{profile}-{index}';trial.mkdir();(trial/'fping.jsonl').write_text('first-run-only\n')
            return {'profile':profile,'route_convergence_ms':index}
        def bad(drill,profile,index):
            trial=drill.out/f'{profile}-{index}';trial.mkdir();(trial/'fping.jsonl').write_text('second-run-only\n');raise RuntimeError('failed new run')
        with tempfile.TemporaryDirectory() as out,patch.object(failover,'Monitor'),contextlib.redirect_stdout(io.StringIO()):
            with patch.object(failover.Drill,'trial',new=good):first=failover.run_campaign(runtime,out)
            summary=(first/'summary.json').read_bytes()
            self.assertEqual(json.loads((first/'run.json').read_text())['status'],'passed')
            with patch.object(failover.Drill,'trial',new=bad),self.assertRaises(RuntimeError):failover.run_campaign(runtime,out)
            runs=sorted(Path(out).glob('run-*'));self.assertEqual(len(runs),2);second=next(run for run in runs if run!=first)
            self.assertEqual((first/'summary.json').read_bytes(),summary)
            self.assertFalse((second/'summary.json').exists())
            self.assertEqual((first/'baseline-1/fping.jsonl').read_text(),'first-run-only\n')
            self.assertEqual((second/'baseline-1/fping.jsonl').read_text(),'second-run-only\n')
            self.assertEqual(json.loads((second/'run.json').read_text())['status'],'failed')
    def test_failure_after_success_file_write_removes_that_runs_markers(self):
        # A Monitor can surface a counter failure from __exit__ after commands
        # finished. Failed run metadata must not coexist with its PASS artifacts.
        for module,runner,artifact in ((baseline,'run_baseline','acceptance.json'),(failover,'run_campaign','summary.json')):
            runtime=MagicMock();runtime.run.return_value='{}'
            monitor=MagicMock();monitor.return_value.__exit__.side_effect=RuntimeError('late monitor failure')
            def trial(drill,profile,index):return {'profile':profile,'route_convergence_ms':index}
            with tempfile.TemporaryDirectory() as out,contextlib.redirect_stdout(io.StringIO()),patch.object(module,'Monitor',monitor),patch.object(baseline.Drill,'wait_ready',return_value={}),patch.object(baseline.Drill,'usage'),patch.object(failover.Drill,'trial',new=trial):
                with self.assertRaisesRegex(RuntimeError,'late monitor'):getattr(module,runner)(runtime,out)
                runs=list(Path(out).glob('run-*'));self.assertEqual(len(runs),1)
                self.assertFalse((runs[0]/artifact).exists())
                self.assertEqual(json.loads((runs[0]/'run.json').read_text())['status'],'failed')
    def test_existing_trial_directory_is_not_appended(self):
        runtime=MagicMock()
        with tempfile.TemporaryDirectory() as out:
            trial=Path(out)/'baseline-1';trial.mkdir();raw=trial/'fping.jsonl';raw.write_text('old raw\n')
            drill=failover.Drill(runtime,out)
            with self.assertRaises(FileExistsError):drill.trial('baseline',1)
            self.assertEqual(raw.read_text(),'old raw\n');runtime.command.assert_not_called()
if __name__=='__main__':unittest.main()
