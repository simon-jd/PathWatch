import unittest
import task14_query_api_fixture as fixture
class ObserverTests(unittest.TestCase):
    def test_lateness_relative_to_each_scheduled_slot(self):
        self.assertTrue(fixture.on_time(.001,0))
        self.assertTrue(fixture.on_time(29.001,29))
        self.assertFalse(fixture.on_time(29.251,29))
    def test_complete_expected_payload_not_just_status_or_count(self):
        expected={'count':1,'data':[{'path_key':'owned','samples':'2'}]}
        self.assertTrue(fixture.payload_good(200,b'{"count":1,"data":[{"path_key":"owned","samples":"2"}]}',expected))
        self.assertFalse(fixture.payload_good(200,b'{"count":1,"data":[{"path_key":"wrong","samples":"2"}]}',expected))
        self.assertFalse(fixture.payload_good(503,b'{}',expected))
        self.assertFalse(fixture.payload_good(200,b'{"count":',expected))
    def test_missing_slots_count_as_failures(self):
        self.assertEqual(fixture.interval_summary([{'good':True},{'good':False}],30),{'scheduled':30,'good':1,'failures':29,'missed':28,'slo_achieved':False})
if __name__=='__main__':unittest.main()
