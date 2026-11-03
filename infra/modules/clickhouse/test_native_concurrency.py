"""No Docker calls: exercise lifecycle despite persistent report-write failure."""
import contextlib
import errno
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('native_concurrency', Path(__file__).with_name('native_concurrency.py'))
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class DockerFixture:
    """Subprocess boundary double; unrelated container must survive every case."""
    def __init__(self, failure):
        self.failure = failure
        self.persistently_unwritable = False
        self.failed_writes = 0
        self.owned = None
        self.removed = []
        self.commands = []
        self.foreign = {'Id': 'foreign-id', 'Config': {'Labels': {'project': 'another-project'}}}

    def run(self, argv, **_kwargs):
        self.commands.append(argv)
        code, stdout, stderr = 0, '', ''
        if argv[0] == 'du':
            stdout = '1 fixture\n'
        elif argv[:3] == ['docker', 'image', 'inspect']:
            stdout = json.dumps([{'Id': native.IMAGE.split('@')[1], 'Size': 1183513909, 'RepoDigests': [native.IMAGE]}])
        elif argv[:2] == ['docker', 'create']:
            labels = dict(arg.split('=', 1) for index, arg in enumerate(argv) if argv[index-1] == '--label')
            self.owned = {'Id': 'owned-id', 'Name': argv[argv.index('--name')+1], 'Config': {'Labels': labels},
                          'State': {'Running': False}, 'HostConfig': {key: None for key in
                          ['Memory', 'MemorySwap', 'NanoCpus', 'CapDrop', 'SecurityOpt', 'Tmpfs', 'LogConfig', 'ReadonlyRootfs', 'PortBindings']}}
            if self.failure == 'wrong-labels':
                self.owned['Config']['Labels']['owner'] = 'somebody-else'
            self.persistently_unwritable = self.failure == 'after-create'
            if self.failure == 'create-timeout':
                raise subprocess.TimeoutExpired(argv, 45)
            stdout = 'owned-id\n'
        elif argv[:2] == ['docker', 'start']:
            self.owned['State']['Running'] = True
            self.persistently_unwritable = self.failure == 'after-start'
        elif argv[:2] == ['docker', 'inspect']:
            if self.owned and argv[-1] in (self.owned['Id'], self.owned['Name']):
                stdout = json.dumps([self.owned])
            else:
                if self.failure == 'lowercase-missing':
                    code, stderr = 1, 'error: no such object: ' + argv[-1]
                elif self.failure == 'daemon-after-removal':
                    code, stderr = 1, 'Cannot connect to the Docker daemon: connection refused'
                else:
                    code, stderr = 1, 'Error: No such object: ' + argv[-1]
        elif argv[:2] == ['docker', 'exec']:
            stdout = '253952 /var/lib/clickhouse\n'
        elif argv[:2] == ['docker', 'stats']:
            if self.failure == 'cleanup-interrupt':
                raise KeyboardInterrupt('focused Ctrl-C during cleanup diagnostic')
            stdout = '{}'
        elif argv[:2] == ['docker', 'rm']:
            if self.failure == 'rm-fails':
                code, stderr = 1, 'injected daemon removal error'
            else:
                if not self.owned or argv[-1] != self.owned['Id']:
                    raise AssertionError('cleanup must remove only the inspected owned ID')
                self.removed.append(argv[-1])
                self.owned = None
        elif argv[:2] != ['docker', 'logs']:
            raise AssertionError('unexpected command ' + repr(argv))
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


class NativeLifecycleTests(unittest.TestCase):
    def exercise(self, failure=None):
        docker = DockerFixture(failure)
        original_write = Path.write_text
        (native.ROOT / '.cache').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='task10-cleanup-unit-', dir=native.ROOT / '.cache') as directory:
            root = Path(directory)
            source = root / 'infra/modules/clickhouse'
            source.mkdir(parents=True)
            (source / 'clickhouse.xml').write_text('<clickhouse/>')
            script = source / 'native_concurrency.py'
            script.write_text('# test-local source identity\n')
            output = root / 'result.json'

            def write(path, data, **kwargs):
                if path == output and docker.persistently_unwritable:
                    docker.failed_writes += 1
                    raise OSError(errno.ENOSPC, 'injected persistent evidence failure')
                return original_write(path, data, **kwargs)

            class Opener:
                def open(self, request, **_kwargs):
                    sql = request.data.decode()
                    if sql == 'SELECT 1':
                        value = '1'
                    elif sql.startswith('SELECT count(),uniqExact'):
                        value = '7\t7'
                    elif sql == native.QUERY:
                        value = '7'
                    else:
                        value = ''
                    return io.BytesIO(value.encode())

            stdout, stderr = io.StringIO(), io.StringIO()
            error, result = None, None
            with patch.object(native, 'ROOT', root), patch.object(native, '__file__', str(script)), \
                 patch.object(native.sys, 'argv', [str(script), '--out', str(output)]), \
                 patch.object(native.subprocess, 'run', docker.run), \
                 patch.object(native.signal, 'signal'), \
                 patch.object(native.urllib.request, 'build_opener', return_value=Opener()), \
                 patch.object(Path, 'write_text', write), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    result = native.main()
                except (OSError, KeyboardInterrupt) as caught:
                    error = caught
            summary = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
            report = json.loads(output.read_text())
            retained = list((root / '.cache').glob('task10-concurrency-*'))
            return docker, result, error, summary, report, retained, stderr.getvalue()

    def test_success_keeps_counts_and_cleans_only_owned_container(self):
        docker, code, error, summary, report, retained, _ = self.exercise()
        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(summary['queries_passed'], 8)
        self.assertTrue(summary['evidence_complete'])
        self.assertTrue(summary['port_closed'])
        self.assertEqual(report['raw_rows'], '7\t7')
        self.assertEqual(docker.removed, ['owned-id'])
        self.assertEqual(docker.foreign['Id'], 'foreign-id')
        self.assertFalse(retained)

    def test_real_lowercase_missing_object_confirms_successful_removal(self):
        docker, code, error, summary, _report, retained, _ = self.exercise('lowercase-missing')
        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(docker.removed, ['owned-id'])
        self.assertTrue(summary['container_absent'])
        self.assertTrue(summary['private_directory_removed'])
        self.assertFalse(retained)

    def test_daemon_connection_error_cannot_confirm_absence(self):
        docker, code, error, summary, _report, retained, _ = self.exercise('daemon-after-removal')
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(docker.removed, ['owned-id'])
        self.assertFalse(summary['container_absent'])
        self.assertFalse(summary['private_directory_removed'])
        self.assertTrue(retained)

    def test_cleanup_stats_keyboard_interrupt_still_removes_owned_id(self):
        docker, code, error, summary, _report, retained, _ = self.exercise('cleanup-interrupt')
        self.assertEqual(docker.removed, ['owned-id'])
        self.assertIsNone(docker.owned)
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertTrue(summary['container_absent'])
        self.assertTrue(summary['private_directory_removed'])
        self.assertTrue(summary['lock_released'])
        self.assertTrue(summary['port_closed'])
        self.assertIn('KeyboardInterrupt', ' '.join(summary['cleanup_errors']))
        self.assertEqual(docker.foreign['Id'], 'foreign-id')
        self.assertFalse(retained)

    def assert_persistent_failure_cleanup(self, when):
        docker, code, error, summary, _report, retained, stderr = self.exercise(when)
        self.assertEqual(docker.removed, ['owned-id'])
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertGreaterEqual(docker.failed_writes, 2)
        self.assertFalse(summary['evidence_complete'])
        self.assertTrue(summary['container_absent'])
        self.assertTrue(summary['private_directory_removed'])
        self.assertTrue(summary['lock_released'])
        self.assertTrue(summary['port_closed'])
        self.assertIn('evidence', stderr)
        self.assertFalse(retained)
        self.assertEqual(docker.foreign['Id'], 'foreign-id')

    def test_persistent_save_failure_after_create_still_captures_id_and_cleans(self):
        self.assert_persistent_failure_cleanup('after-create')

    def test_persistent_save_failure_after_start_cannot_block_cleanup(self):
        self.assert_persistent_failure_cleanup('after-start')

    def test_create_timeout_recovers_exact_name_and_labels_before_removing_id(self):
        docker, code, error, summary, _report, retained, _ = self.exercise('create-timeout')
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(docker.removed, ['owned-id'])
        self.assertTrue(summary['container_absent'])
        self.assertFalse(retained)

    def test_wrong_labels_refuse_removal_and_retain_private_directory(self):
        docker, code, error, summary, _report, retained, _ = self.exercise('wrong-labels')
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertFalse(docker.removed)
        self.assertTrue(retained)
        self.assertFalse(summary.get('container_absent', False))
        self.assertFalse(summary.get('private_directory_removed', False))

    def test_failed_removal_never_claims_absence_and_retains_private_directory(self):
        docker, code, error, summary, _report, retained, _ = self.exercise('rm-fails')
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertFalse(docker.removed)
        self.assertTrue(retained)
        self.assertFalse(summary.get('container_absent', False))
        self.assertTrue(summary['cleanup_errors'])


if __name__ == '__main__':
    unittest.main()
