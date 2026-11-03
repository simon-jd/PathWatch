import importlib.util,json,pathlib,tempfile,unittest
P=pathlib.Path(__file__).with_name('capacity_campaign.py')
class CampaignTests(unittest.TestCase):
 def module(self):
  self.assertTrue(P.exists(),'fixed campaign deadline state missing')
  s=importlib.util.spec_from_file_location('campaign',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
 def draft(self,m,base,now=1000000):
  return m.prepare(base,'campaign-1','a'*64,{'home-ubuntu':1000010,'azure-a':1000010,'azure-b':1001010},now)
 def test_readonly_future_start_resume_and_deadline(self):
  m=self.module()
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);s=self.draft(m,base)
   self.assertFalse((base/'capacity-campaign.json').exists())
   self.assertEqual(m.remaining(s,1000000),[3024000,3024000,172800])
   s=m.claim(base,s,1000000);self.assertEqual(s['home_starts'],1)
   (base/'slo-state.json').write_text('{}')
   later=self.draft(m,base,1003610);self.assertEqual(later['home_starts'],1)
   self.assertEqual(m.remaining(later,1003610),[3020400,3020400,170200])
   resumed=m.claim(base,later,1003610);self.assertEqual(resumed['home_starts'],2)
   self.assertEqual(resumed['sites'],s['sites'])
   with self.assertRaises(ValueError):m.remaining(resumed,4024010)
 def test_past_missing_corrupt_changed_clock_and_budget_fail_closed(self):
  m=self.module()
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp)
   with self.assertRaises(ValueError):self.draft(m,base,1000011)
   (base/'journal').mkdir();(base/'journal'/'owner.json').write_text('{}')
   with self.assertRaises(ValueError):self.draft(m,base)
   (base/'journal'/'owner.json').unlink();s=m.claim(base,self.draft(m,base),1000000)
   with self.assertRaises(ValueError):m.prepare(base,'campaign-1','b'*64,{'home-ubuntu':1000010,'azure-a':1000010,'azure-b':1001010},1000000)
   with self.assertRaises(ValueError):self.draft(m,base,999990)
   for _ in range(32):s=m.claim(base,s,1000000)
   with self.assertRaises(ValueError):m.claim(base,s,1000000)
   (base/'capacity-campaign.json').write_text('{}')
   with self.assertRaises(ValueError):self.draft(m,base)

class DeadlineAlarmTests(unittest.TestCase):
 def test_deadline_interrupts_blocked_work_and_state_is_bounded(self):
  import time,signal
  s=importlib.util.spec_from_file_location('campaign',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  self.assertTrue(hasattr(m,'arm_deadline'),'runtime must stop at deadline even during a scan')
  old=m.arm_deadline({'sites':{'home-ubuntu':{'end':time.time()+0.05}}})
  try:
   with self.assertRaises(TimeoutError):time.sleep(1)
  finally:m.disarm_deadline(old)
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ValueError):m.persist(pathlib.Path(tmp),{'oversize':'x'*16384})

class CampaignClockTests(unittest.TestCase):
 def test_delayed_first_claim_clock_jump_and_expiry_preserve_state(self):
  from unittest.mock import patch
  s=importlib.util.spec_from_file_location('campaign',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);draft=m.prepare(base,'one','a'*64,{'home-ubuntu':1000010,'azure-a':1000010,'azure-b':1000010},1000000)
   with self.assertRaises(ValueError):m.claim(base,draft,1000011)
   state=m.claim(base,draft,1000000);raw=(base/'capacity-campaign.json').read_bytes()
   with patch.object(m.time,'time',return_value=1000100),patch.object(m.time,'monotonic',return_value=1):
    with self.assertRaises(ValueError):m.checkpoint(base,state,1000000,0)
   self.assertEqual((base/'capacity-campaign.json').read_bytes(),raw)
   with patch.object(m.time,'time',return_value=4024010),patch.object(m.time,'monotonic',return_value=3024010):
    with self.assertRaises(ValueError):m.checkpoint(base,state,1000000,0)
   self.assertEqual((base/'capacity-campaign.json').read_bytes(),raw)

class MissingHistoryTests(unittest.TestCase):
 def test_failed_first_start_cannot_create_empty_sli_after_window_begins(self):
  s=importlib.util.spec_from_file_location('campaign',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);starts={'home-ubuntu':1000010,'azure-a':1000010,'azure-b':1000010}
   draft=m.prepare(base,'one','a'*64,starts,1000000);state=m.claim(base,draft,1000000)
   self.assertEqual(m.prepare(base,'one','a'*64,starts,1000001)['sites'],state['sites'])
   with self.assertRaises(ValueError):m.prepare(base,'one','a'*64,starts,1000011)
   self.assertEqual(m.load(base)['sites'],state['sites'])

if __name__=='__main__':unittest.main()
