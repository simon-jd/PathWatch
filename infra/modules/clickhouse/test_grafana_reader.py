"""Inspect real generated configuration; native permission checks run separately."""
import contextlib
import hashlib
import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import home


class ConfigurationWritten(Exception):
    pass


class GrafanaReaderTests(unittest.TestCase):
    def generate(self, reader='test-reader<&>password'):
        # A missing/expanded constraint must fail these tests at the XML boundary.
        # Stop before naming/starting services, keeping config writes and parsing real.
        with tempfile.TemporaryDirectory(dir=home.ROOT / '.cache') as tmp, contextlib.ExitStack() as stack:
            base = pathlib.Path(tmp)
            bins = base / 'bin'
            bins.mkdir()
            hashes = {}
            for kind in ('ingest', 'probe', 'echo'):
                (bins / kind).write_text(kind)
                hashes[kind] = hashlib.sha256(kind.encode()).hexdigest()
            cfg = {'listen': '127.0.0.1:19443', 'development_cleartext': True,
                   'binary_dir': str(bins), 'binary_sha256': hashes}
            stack.enter_context(patch.dict(home.os.environ, {
                'PATHWATCH_CLICKHOUSE_PASSWORD': 'test-ingest-password',
                'PATHWATCH_GRAFANA_CH_PASSWORD': reader}))
            stack.enter_context(patch.object(home, 'allocated', return_value=0))
            stack.enter_context(patch.object(home.uuid, 'uuid4', side_effect=ConfigurationWritten))
            real_open = open

            def owned_open(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if path == base / 'owner.lock':
                    stack.callback(handle.close)
                return handle

            stack.enter_context(patch.object(home, 'open', side_effect=owned_open, create=True))
            with self.assertRaises(ConfigurationWritten):
                home.run(base, cfg)
            output = base / 'users.xml'
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            return ET.parse(output).getroot()

    def test_reader_has_only_bounded_execution_time_exception(self):
        xml = self.generate()
        user = xml.find('users/pathwatch_grafana')
        self.assertIsNotNone(user)
        self.assertEqual(user.findtext('password'), 'test-reader<&>password')
        profile = xml.find('profiles/' + user.findtext('profile'))
        self.assertIsNotNone(profile)
        self.assertEqual(profile.findtext('readonly'), '1')
        self.assertEqual(profile.findtext('max_execution_time'), '10')
        self.assertEqual(profile.findtext('max_memory_usage'), '67108864')
        constraints = profile.find('constraints')
        self.assertIsNotNone(constraints, 'reader must allow the driver deadline setting')
        self.assertEqual([node.tag for node in constraints], ['max_execution_time'])
        execution = constraints.find('max_execution_time')
        self.assertEqual(execution.findtext('min'), '1')
        self.assertEqual(execution.findtext('max'), '15')
        self.assertEqual(sorted(node.tag for node in execution),
                         ['changeable_in_readonly', 'max', 'min'])
        self.assertEqual(len(xml.findall('.//changeable_in_readonly')), 1)
        self.assertIsNone(user.find('access_management'))

    def test_reader_is_absent_without_opt_in_password(self):
        xml = self.generate(reader='')
        self.assertIsNone(xml.find('users/pathwatch_grafana'))
        self.assertIsNone(xml.find('profiles/pathwatch_readonly'))
        self.assertEqual(xml.findtext('users/pathwatch_ingest/profile'), 'default')

    def test_server_enables_readonly_constraint_semantics(self):
        xml = ET.parse(pathlib.Path(home.__file__).with_name('clickhouse.xml')).getroot()
        self.assertEqual(xml.findtext('access_control_improvements/settings_constraints_replace_previous'), 'true')


if __name__ == '__main__':
    unittest.main()
