"""FRR summary compatibility using the controller's sanitized actual snapshot."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import hybrid_runtime

class FrrSummaryTests(unittest.TestCase):
    def setUp(self):
        self.actual=json.loads((Path(__file__).parent/'fixtures/frr844-routing.json').read_text())
        now=max(stamp for site in self.actual.values() for stamp in site['handshake_epochs'])+1
        self.clock=patch.object(hybrid_runtime.time,'time',return_value=now);self.clock.start();self.addCleanup(self.clock.stop)
    def wrapped(self):
        snapshot=copy.deepcopy(self.actual)
        for site in snapshot.values():site['bgp']={'ipv4Unicast':site['bgp']}
        return snapshot
    def test_actual_frr844_direct_address_family_summary_is_ready(self):
        self.assertTrue(hybrid_runtime.established(self.actual))
    def test_existing_wrapped_summary_remains_ready(self):
        self.assertTrue(hybrid_runtime.established(self.wrapped()))
    def test_mixed_invalid_wrapped_summary_does_not_fall_back_to_valid_direct(self):
        for invalid in (None,[],{}, {'peers':None},{'peers':[]},{'peers':{}}, {'peers':{'one':{'state':'Idle'},'two':{'state':'Established'}}}):
            snapshot=copy.deepcopy(self.actual);snapshot['home']['bgp']['ipv4Unicast']=invalid
            with self.subTest(invalid=invalid):self.assertFalse(hybrid_runtime.established(snapshot))
    def test_valid_wrapped_summary_keeps_priority_over_invalid_direct(self):
        snapshot=self.wrapped();snapshot['home']['bgp']['peers']=None
        self.assertTrue(hybrid_runtime.established(snapshot))
    def test_malformed_missing_peer_maps_counts_and_states_are_not_ready(self):
        invalid=(None,[],{},'invalid',{'peers':None},{'peers':[]},{'peers':{}},
                 {'peers':{'one':{'state':'Established'}}},
                 {'peers':{str(i):{'state':'Established'} for i in range(3)}},
                 {'peers':{'one':None,'two':{'state':'Established'}}},
                 {'peers':{'one':{},'two':{'state':'Established'}}},
                 {'peers':{'one':{'state':'Idle'},'two':{'state':'Established'}}})
        for wrapped in (False,True):
            for value in invalid:
                snapshot=copy.deepcopy(self.actual);snapshot['azure']['bgp']={'ipv4Unicast':value} if wrapped else value
                with self.subTest(wrapped=wrapped,value=value):self.assertFalse(hybrid_runtime.established(snapshot))
    def test_both_sites_still_need_routes_and_two_recent_wireguard_handshakes(self):
        for wrapped in (False,True):
            for site in ('home','azure'):
                for field,value in (('route',{}),('kernel',[{'dev':'eth0'}]),('handshake_epochs',[]),('handshake_epochs',[1,1]),('handshake_epochs',[0,0])):
                    snapshot=self.wrapped() if wrapped else copy.deepcopy(self.actual);snapshot[site][field]=value
                    with self.subTest(wrapped=wrapped,site=site,field=field,value=value):self.assertFalse(hybrid_runtime.established(snapshot))
    def test_bfd_requirement_is_not_bypassed_by_summary_normalization(self):
        self.assertFalse(hybrid_runtime.established(self.actual,require_bfd=True))
        for wrapped in (False,True):
            snapshot=self.wrapped() if wrapped else copy.deepcopy(self.actual)
            for site in snapshot.values():site['bfd']=[{'status':'up'},{'status':'up'}]
            self.assertTrue(hybrid_runtime.established(snapshot,require_bfd=True))
            snapshot['home']['bfd'][1]['status']='down'
            self.assertFalse(hybrid_runtime.established(snapshot,require_bfd=True))
if __name__=='__main__':unittest.main()
