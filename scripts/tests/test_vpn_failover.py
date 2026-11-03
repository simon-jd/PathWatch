import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,MagicMock
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import vpn_failover as failover
from hybrid_usage import enforce,snapshot
from hybrid_preflight import check_routes
class ParserTests(unittest.TestCase):
    def rows(self):
        return [{'line':f'[1700000000.{i}] 10.10.0.4 : [{i}], '+('timed out (waiting for response)' if i in (1,2) else '84 bytes, 1.00 ms (1.00 avg, 0% loss)'),'monotonic_ns':1000000000+i*100000000,'realtime_ns':1700000000000000000+i*100000000} for i in range(5)]
    def test_measured_loss_and_recovery_use_monotonic_not_hold_timer(self):
        rows=self.rows();rows[3]['realtime_ns']-=5000000000
        result=failover.parse_fping(rows,1050000000)
        self.assertEqual(result['loss_count'],2);self.assertEqual(result['loss_intervals'][0]['loss_interval_ms'],200);self.assertEqual(result['loss_intervals'][0]['recovery_from_impair_ms'],250)
    def test_missing_packet_sequence_cannot_silently_hide_loss(self):
        rows=self.rows();rows.pop(2)
        with self.assertRaisesRegex(ValueError,"Missing"):failover.parse_fping(rows,1)
    def test_trailing_loss_is_unrecovered(self):
        result=failover.parse_fping(self.rows()[:3],1050000000)
        self.assertIsNone(result['loss_intervals'][0]['recovered_seq'])
    def test_no_actual_data_duplicate_and_nonmonotonic_refused(self):
        for rows in ([],self.rows()+[self.rows()[0]],list(reversed(self.rows()))):
            if rows and rows==list(reversed(self.rows())):rows[0]['monotonic_ns']=0
            with self.assertRaises(ValueError):failover.parse_fping(rows,1)
class CleanupTests(unittest.TestCase):
    def test_all_fifteen_trials_and_actual_medians(self):
        runtime=MagicMock()
        with tempfile.TemporaryDirectory() as out:
            drill=failover.Drill(runtime,out)
            seen=[]
            def trial(profile,index):
                seen.append((profile,index));return {'profile':profile,'route_convergence_ms':index*10}
            drill.trial=trial
            result=drill.run()
            self.assertEqual(len(seen),15);self.assertEqual(result['medians_route_convergence_ms']['bfd'],30)
            runtime.impair.assert_called_with(False);runtime.profile.assert_called_with('baseline')
    def test_cancel_and_failed_command_restore_underlay_and_profile(self):
        for exception in (KeyboardInterrupt(),RuntimeError('failed command'),TimeoutError()):
            runtime=MagicMock()
            with tempfile.TemporaryDirectory() as out:
                drill=failover.Drill(runtime,out);drill.trial=MagicMock(side_effect=exception)
                with self.assertRaises(type(exception)):drill.run()
                runtime.impair.assert_called_with(False);runtime.profile.assert_called_with('baseline')
    def test_trial_failure_and_cancel_restore_before_any_probe(self):
        for exception in (KeyboardInterrupt(),RuntimeError('BGP command failed')):
            runtime=MagicMock()
            with tempfile.TemporaryDirectory() as out:
                drill=failover.Drill(runtime,out);drill.wait_ready=MagicMock(side_effect=exception)
                with self.assertRaises(type(exception)):drill.trial('baseline',1)
                self.assertGreaterEqual(runtime.impair.call_count,2);runtime.impair.assert_called_with(False)
    def test_failure_or_cancel_with_live_probe_restores_after_impairment(self):
        for error in (RuntimeError('route command failed'),KeyboardInterrupt()):
            runtime=MagicMock();runtime.home_route.side_effect=error
            process=MagicMock();process.stdout=[];process.poll.return_value=None
            with tempfile.TemporaryDirectory() as out:
                drill=failover.Drill(runtime,out);drill.wait_ready=MagicMock(return_value={});drill.usage=MagicMock()
                with patch.object(failover.subprocess,'Popen',return_value=process),patch.object(failover.time,'sleep'),self.assertRaises(type(error)):
                    drill.trial('baseline',1)
                self.assertIn(unittest.mock.call(True),runtime.impair.call_args_list)
                runtime.impair.assert_called_with(False);runtime.stop_probe.assert_called_once();process.terminate.assert_called_once()
    def test_time_limits_fail_closed(self):
        for trial,poll in ((301,240),(230,901),(0,1)):
            with tempfile.TemporaryDirectory() as out,self.assertRaises(ValueError):failover.Drill(MagicMock(),out,trial,poll)
class UsageTests(unittest.TestCase):
    def test_monitor_keeps_twenty_percent_cleanup_headroom(self):
        sample={'disks':{'sda':{'bounded_transactions':10000}},'tx_bytes':{'eth0':1000}}
        self.assertEqual(enforce(sample)['disk_transactions_upper_bound'],10000)
        for field in ('io','net'):
            sample={'disks':{'sda':{'bounded_transactions':800000 if field=='io' else 100}},'tx_bytes':{'eth0':1600000000 if field=='net' else 100}}
            with self.assertRaisesRegex(ValueError,'80%'):enforce(sample)
    def test_large_io_split_conservative_and_boot_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);disk=root/'class/block/sda';disk.mkdir(parents=True);(disk/'stat').write_text('10 0 1024 0 20 0 1024 0 0 0 0')
            nic=root/'class/net/eth0/statistics';nic.mkdir(parents=True);(nic/'tx_bytes').write_text('1234')
            result=snapshot(root);self.assertEqual(result['disks']['sda']['bounded_transactions'],94);self.assertEqual(result['tx_bytes']['eth0'],1234)
    def test_host_overlap_no_default_route_mutation(self):
        check_routes([{'dst':'default'},{'dst':'10.0.0.0/24'},{'dst':'172.17.0.0/16'}])
        with self.assertRaises(ValueError):check_routes([{'dst':'10.10.1.0/24'}])
        check_routes([{'dst':'10.30.0.0/16'}],True)
if __name__=='__main__':unittest.main()
