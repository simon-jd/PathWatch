"""Read-only reconciliation evidence never repairs or writes a ledger."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from test_azure_parent_renewal import fixture,NOW,SUB
ROOT=Path(__file__).resolve().parents[2]

class IdentityTests(unittest.TestCase):
    def test_exact_current_arm_and_state_agree_before_identity_evidence(self):
        path=ROOT/'scripts/bootstrap_identity_evidence.py';self.assertTrue(path.is_file(),'missing read-only identity reconciliation evidence entrypoint')
        spec=importlib.util.spec_from_file_location('identity',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as tmp:
            plan,_,_=fixture(Path(tmp));resources=[dict(address=r['address'],mode='managed',type=r['type'],values=r['change']['before']) for r in plan['resource_changes']]
        state={'values':{'root_module':{'resources':resources}}}
        current={'subscription_id':SUB,'captured_at':'2026-10-24T19:59:00Z','resources':[{'id':r['values']['id'],'tags':r['values'].get('tags',{})} for r in resources]}
        result=module.build(state,current,SUB,NOW);self.assertEqual(len(result['identities']),4);self.assertTrue(result['requires_controller_reconciliation'])
        for change in ('foreign','tags','missing','stale'):
            value=copy.deepcopy(current)
            if change=='foreign':value['resources'][1]['id']='foreign'
            if change=='tags':value['resources'][0]['tags']['ttl']='2027-01-01T00:00:00Z'
            if change=='missing':value['resources'].pop()
            if change=='stale':value['captured_at']='2026-10-23T19:59:00Z'
            with self.subTest(change=change),self.assertRaises(ValueError):module.build(state,value,SUB,NOW)
if __name__=='__main__':unittest.main()
