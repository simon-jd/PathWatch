"""Real admit/lock/persistence entry regressions against isolated temporary ledgers."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import azure_budget as budget
import azure_dns_scope as scope
import azure_reaper as reaper
from azure_teardown import validate as teardown
from dns_proposal import proposal

SUB='11111111-1111-1111-1111-111111111111'
DIGEST='a'*64
NOW=dt.datetime(2026,10,24,6,30,tzinfo=dt.timezone.utc)
class Clock(dt.datetime):
    @classmethod
    def now(cls,tz=None):return NOW

class DNSAdmissionFix3(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads((Path(__file__).parent/'fixtures/dns-native-plan.json').read_text())
        self.proposal=proposal(self.plan,'2026-10-24T06:00:00Z')
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'ledger.json'
        for patcher in (patch.object(budget,'LEDGER',self.path),patch.dict(os.environ,AZURE_SUBSCRIPTION_ID=SUB),patch.object(budget.dt,'datetime',Clock)):
            patcher.start();self.addCleanup(patcher.stop)
        self.output=io.StringIO();redirect=contextlib.redirect_stdout(self.output);redirect.__enter__();self.addCleanup(redirect.__exit__,None,None,None)
    def write(self,reserved=False,block=False):
        rows=copy.deepcopy(self.proposal['resources']) if reserved else []
        for row in rows:row.update(status='reserved',plan_sha256=DIGEST,deleted_utc=None)
        ledger={'schema':1,'resources':rows,'reconciliation_block':block,'audit_metadata':{'review':'must survive remainder'}}
        self.path.write_text(json.dumps(ledger));return ledger
    def assert_blocked_without_validation_or_write(self,command,plan,rows):
        before=self.path.read_bytes()
        with patch.object(budget,'check',wraps=budget.check) as check,self.assertRaisesRegex(ValueError,'reconciliation'):
            budget.admit(command,plan,rows,DIGEST)
        self.assertEqual(check.call_count,0)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertNotIn('PASS',self.output.getvalue())
    def test_reserved_check_freeze_precedes_remainder_and_pass(self):
        self.write(reserved=True,block={'reason':'unknown final charges'})
        self.assert_blocked_without_validation_or_write('check',self.plan,self.proposal)
        self.write(reserved=True)
        before=self.path.read_bytes();budget.admit('check',self.plan,self.proposal,DIGEST)
        self.assertEqual(self.path.read_bytes(),before);self.assertIn('PASS',self.output.getvalue())
    def test_create_reserve_and_duplicate_reserve_freeze_before_branches(self):
        for reserved in (False,True):
            with self.subTest(reserved=reserved):
                self.write(reserved=reserved,block=True)
                self.assert_blocked_without_validation_or_write('reserve',self.plan,self.proposal)
        self.write();budget.admit('reserve',self.plan,self.proposal,DIGEST)
        ledger=json.loads(self.path.read_text());self.assertEqual(len(ledger['resources']),39)
        self.assertEqual(ledger['audit_metadata'],{'review':'must survive remainder'})
    def test_noop_check_and_reserve_freeze_before_validation(self):
        for r in self.plan['resource_changes']:
            if r.get('mode')=='managed':r['change']['actions']=['no-op']
        for command in ('check','reserve'):
            with self.subTest(command=command):
                self.write(reserved=True,block=True)
                self.assert_blocked_without_validation_or_write(command,self.plan,{'resources':[]})
        self.write(reserved=True);before=self.path.read_bytes()
        for command in ('check','reserve'):budget.admit(command,self.plan,{'resources':[]},DIGEST)
        self.assertEqual(self.path.read_bytes(),before)
    def test_reserved_remainder_preserves_all_root_metadata(self):
        ledger=self.write(reserved=True)
        with patch.object(budget,'check',wraps=budget.check) as check:budget.admit('check',self.plan,self.proposal,DIGEST)
        self.assertEqual(check.call_args.args[2],{**ledger,'resources':[]})
    def test_freeze_does_not_disable_teardown_or_reaper(self):
        self.write(reserved=True,block=True);before=self.path.read_bytes()
        plan=copy.deepcopy(self.plan)
        plan['resource_changes']=[r for r in plan['resource_changes'] if r['type']!='azurerm_private_dns_zone_virtual_network_link']
        for r in plan['resource_changes']:
            if r.get('mode')!='managed':continue
            v=r['change']['after'];v['id']=scope.resource_ids(SUB)[r['address']]
            r['change']={'actions':['delete'],'before':v,'after':None}
        rid=scope.resource_ids(SUB)[scope.address('azurerm_linux_virtual_machine','a')]
        expired={'id':rid,'tags':{'project':'pathwatch','owner':'codex-pathwatch','ttl':'2026-10-24T06:00:00Z'}}
        responses=[{'id':SUB,'state':'Enabled','name':'Azure for Students'},True,[expired],None]
        with patch.object(budget,'check',side_effect=AssertionError('cleanup must not use admission')),patch.object(reaper,'SUB',SUB,create=True),patch.object(reaper,'az',side_effect=responses) as az:
            teardown(plan,SUB);reaper.main('reap')
        az.assert_called_with('resource','delete','--ids',rid)
        self.assertEqual(self.path.read_bytes(),before)

if __name__=='__main__':unittest.main()
