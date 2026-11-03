#!/usr/bin/env python3
"""Real, isolated regression for global-pool starvation of eight MV checks."""
import argparse
import base64
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from disk_share import DISK_SHARE

ROOT = Path(__file__).resolve().parents[3]
IMAGE = 'clickhouse/clickhouse-server@sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841'
ACCOUNTED_IMAGES = 1183513909 + 371925065 + 550000000
RUNTIME_RESERVE = (50 + 16 + 8 + 2) * 1024 * 1024
QUERY = "SELECT countMerge(samples) FROM pathwatch.path_quantiles_5m WHERE batch_key='fixture'"
SETUP = [
    'CREATE DATABASE pathwatch',
    'CREATE TABLE pathwatch.probe_results (batch_key String, result_index UInt8) ENGINE=MergeTree ORDER BY (batch_key,result_index)',
    'CREATE MATERIALIZED VIEW pathwatch.path_quantiles_5m ENGINE=AggregatingMergeTree ORDER BY batch_key AS SELECT batch_key,countState() AS samples FROM pathwatch.probe_results GROUP BY batch_key',
    "INSERT INTO pathwatch.probe_results SELECT 'fixture',number FROM numbers(7)",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='new JSON evidence file inside the checkout')
    parser.add_argument('--port', type=int, default=18124)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be unprivileged')
    out = args.out.resolve()
    if not out.is_relative_to(ROOT) or out.exists():
        parser.error('--out must be a new file inside the checkout')
    out.parent.mkdir(parents=True, exist_ok=True)
    # Refuse overwrites even if another invocation races the initial check.
    with out.open('x') as handle:
        handle.write('{}\n')
    report = {'argv': sys.argv, 'start_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'commands': [], 'query': QUERY, 'deadline_seconds': 30, 'queries': [], 'evidence_complete': True}
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    lock = (cache / 'task10-concurrency.lock').open('a')
    work = None
    container_id = None
    container_name = None
    create_attempted = False
    labels = {}
    exit_code = 1

    def save():
        try:
            out.write_text(json.dumps(report, indent=2) + '\n')
        except OSError as error:
            report['evidence_complete'] = False
            report.setdefault('evidence_errors', []).append(f'{type(error).__name__}: {error}')
            raise

    def command(argv, check=True, persist=True):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
        report['commands'].append({'argv': argv, 'exit': result.returncode,
                                   'stdout': result.stdout, 'stderr': result.stderr})
        if persist:
            save()
        if check and result.returncode:
            raise RuntimeError(f'{argv[:2]} exited {result.returncode}; see commands in report')
        return result

    def inspect_container(target):
        # Cleanup must work even when every evidence write continues to fail.
        result = command(['docker', 'inspect', target], check=False, persist=False)
        if result.returncode:
            missing = {f'{prefix}: no such {kind}: {target}'.casefold()
                       for prefix in ('error', 'error response from daemon') for kind in ('object', 'container')}
            if result.stderr.strip().casefold() in missing:
                return None
            raise RuntimeError('cannot establish container absence: ' + result.stderr.strip())
        return json.loads(result.stdout)[0]

    def interrupted(signum, _frame):
        raise InterruptedError(f'signal {signum}')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', args.port))
        allocated = int(command(['du', '-s', '--block-size=1', str(ROOT)]).stdout.split()[0])
        report['budget'] = {'checkout_allocated_before': allocated, 'accounted_images_builder_bytes': ACCOUNTED_IMAGES,
                            'runtime_reserve_bytes': RUNTIME_RESERVE, 'limit_bytes': DISK_SHARE}
        if allocated + ACCOUNTED_IMAGES + RUNTIME_RESERVE > DISK_SHARE:
            raise RuntimeError('project budget cannot reserve bounded fixture')
        image = json.loads(command(['docker', 'image', 'inspect', IMAGE]).stdout)[0]
        report['image'] = {'id': image['Id'], 'size': image['Size'], 'repo_digests': image['RepoDigests']}
        work = Path(tempfile.mkdtemp(prefix='task10-concurrency-', dir=cache))
        work.chmod(0o700)
        config = ROOT / 'infra/modules/clickhouse/clickhouse.xml'
        shutil.copyfile(config, work / 'config.xml')
        report['source_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in [config, Path(__file__).resolve()]}
        password = secrets.token_hex(24)
        # Private, per-run credentials are neither command arguments nor evidence.
        users = ('<clickhouse><users><default remove="remove"/><pathwatch_fixture><password>' + password +
                 '</password><networks><ip>::/0</ip></networks><profile>default</profile><quota>default</quota>'
                 '</pathwatch_fixture></users></clickhouse>')
        (work / 'users.xml').write_text(users)
        (work / 'users.xml').chmod(0o600)
        auth = 'Basic ' + base64.b64encode(('pathwatch_fixture:' + password).encode()).decode()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def query(sql):
            request = urllib.request.Request(
                f'http://127.0.0.1:{args.port}/?max_execution_time=30&max_memory_usage=67108864&max_threads=2',
                data=sql.encode(), headers={'Authorization': auth})
            try:
                with opener.open(request, timeout=35) as response:
                    return response.read(65536).decode().strip()
            except urllib.error.HTTPError as error:
                # Preserve SQL diagnosis, including the server error body.
                body = error.read(65536).decode(errors='replace')
                raise RuntimeError(f'HTTP {error.code}: {body}') from None

        token = work.name.removeprefix('task10-concurrency-')
        labels = {'project': 'pathwatch', 'owner': 'codex-pathwatch', 'task': '10-recovery-regression', 'run': token}
        container_name = 'pathwatch-task10-concurrency-' + token
        argv = ['docker', 'create', '--pull=never', '--name', container_name,
                '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '2g', '--memory-swap', '2g', '--cpus', '2',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--read-only',
                '--log-opt', 'max-size=2m', '--log-opt', 'max-file=1']
        for mount, size in [('/var/lib/clickhouse', '50m'), ('/var/log/clickhouse-server', '16m'), ('/tmp', '8m')]:
            argv += ['--tmpfs', f'{mount}:rw,size={size},uid={os.getuid()},gid={os.getgid()}']
        argv += ['-p', f'127.0.0.1:{args.port}:8123', '-v', f'{work}/config.xml:/etc/clickhouse-server/config.d/pathwatch.xml:ro',
                 '-v', f'{work}/users.xml:/etc/clickhouse-server/users.d/pathwatch.xml:ro']
        for key, value in labels.items():
            argv += ['--label', f'{key}={value}']
        argv += ['--entrypoint', 'clickhouse-server', IMAGE, '--config-file=/etc/clickhouse-server/config.xml']
        # A timeout can occur after Docker creates the object. Only this exact
        # pre-generated nonce name + labels may recover its ID in finally.
        create_attempted = True
        container_id = command(argv, persist=False).stdout.strip()
        report['container_id'] = container_id
        save()
        command(['docker', 'start', container_id])
        for attempt in range(60):
            try:
                if query('SELECT 1') == '1':
                    break
            except (OSError, RuntimeError) as error:
                report['last_startup_error'] = str(error)
                info = json.loads(command(['docker', 'inspect', container_id]).stdout)[0]
                if not info['State']['Running']:
                    raise RuntimeError('fixture ClickHouse exited before readiness') from error
                if attempt == 59:
                    raise
                time.sleep(1)
        report['setup'] = []
        for sql in SETUP:
            report['setup'].append({'sql': sql, 'result': query(sql)})
        report['version'] = query('SELECT version()')
        report['settings'] = query("SELECT name,value FROM system.server_settings WHERE name IN ('max_thread_pool_size','max_thread_pool_free_size','thread_pool_queue_size','max_server_memory_usage') FORMAT JSONEachRow")
        metrics = "SELECT metric,value FROM system.metrics WHERE metric IN ('GlobalThread','GlobalThreadActive','GlobalThreadScheduled') FORMAT JSONEachRow"
        report['initial_threads'] = query(metrics)
        barrier = threading.Barrier(8)

        def check(index):
            barrier.wait(timeout=10)
            started = time.monotonic()
            result = {'index': index}
            try:
                result['result'] = query(QUERY)
            except (OSError, RuntimeError) as error:
                result['error'] = str(error)
            result['seconds'] = time.monotonic() - started
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            report['queries'] = list(executor.map(check, range(8)))
        report['final_threads'] = query(metrics)
        report['raw_rows'] = query('SELECT count(),uniqExact(result_index) FROM pathwatch.probe_results')
        report['mv_samples'] = query(QUERY)
        if any(item.get('result') != '7' or item['seconds'] >= 30 for item in report['queries']):
            raise AssertionError('eight concurrent MV checks must each return 7 within 30 seconds')
        if report['raw_rows'] != '7\t7' or report['mv_samples'] != '7':
            raise AssertionError('fixture raw/MV counts or physical uniqueness changed')
        exit_code = 0
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        cleanup_errors = []
        report['container_absent'] = not create_attempted
        report['private_directory_removed'] = work is None
        if create_attempted:
            try:
                info = inspect_container(container_id or container_name)
                if info is None:
                    report['container_absent'] = True
                else:
                    if ((container_id and info['Id'] != container_id) or
                        info['Name'].lstrip('/') != container_name or
                        not all(info['Config']['Labels'].get(key) == value for key, value in labels.items())):
                        raise RuntimeError('refusing cleanup: exact owned ID/name/labels do not match')
                    container_id = info['Id']
                    report['container_id'] = container_id
                    try:
                        report['container_limits'] = {key: info['HostConfig'][key] for key in
                                                      ['Memory', 'MemorySwap', 'NanoCpus', 'CapDrop', 'SecurityOpt', 'Tmpfs', 'LogConfig', 'ReadonlyRootfs', 'PortBindings']}
                        if info['State']['Running']:
                            data = command(['docker', 'exec', container_id, 'du', '-s', '-B1', '/var/lib/clickhouse'], persist=False)
                            report['data_allocated_bytes'] = int(data.stdout.split()[0])
                            report['docker_stats'] = command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', container_id], persist=False).stdout.strip()
                        command(['docker', 'logs', '--tail', '100', container_id], check=False, persist=False)
                    except Exception as error:
                        cleanup_errors.append('cleanup diagnostics: ' + str(error))
                    except KeyboardInterrupt as error:
                        # Delay this cancellation until owned cleanup and the
                        # directory/lock/port checks finish; it remains failure.
                        cleanup_errors.append('cleanup diagnostics interrupted: KeyboardInterrupt: ' + str(error))
                    finally:
                        # Only reached after exact ownership verification. Even
                        # a BaseException in diagnostics must attempt this rm.
                        try:
                            command(['docker', 'rm', '--force', container_id], persist=False)
                        except Exception as error:
                            cleanup_errors.append('owned removal: ' + str(error))
                        report['container_absent'] = inspect_container(container_id) is None
                        if not report['container_absent']:
                            raise RuntimeError('owned container remains')
            except Exception as error:
                cleanup_errors.append(str(error))
        if work:
            if report['container_absent']:
                try:
                    shutil.rmtree(work)
                    report['private_directory_removed'] = not work.exists()
                except OSError as error:
                    cleanup_errors.append('private directory removal: ' + str(error))
            else:
                report['retained_private_directory'] = str(work)
        # Never remove the stable flock inode: a waiting process may hold it.
        try:
            lock.close()
            with (cache / 'task10-concurrency.lock').open('a') as verify_lock:
                fcntl.flock(verify_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                report['lock_released'] = True
        except OSError as error:
            cleanup_errors.append('fixture lock release check: ' + str(error))
        try:
            with socket.socket() as sock:
                report['port_closed'] = sock.connect_ex(('127.0.0.1', args.port)) != 0
            if not report['port_closed']:
                cleanup_errors.append('fixture loopback port remains open')
        except OSError as error:
            cleanup_errors.append('fixture port check: ' + str(error))
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
            exit_code = 1
        if not report['evidence_complete']:
            exit_code = 1
        report['end_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        report['exit'] = exit_code
        try:
            save()
        except OSError:
            exit_code = report['exit'] = 1
        if not report['evidence_complete']:
            print('evidence persistence failed; JSON may be incomplete: ' + report['evidence_errors'][-1], file=sys.stderr)
    print(json.dumps({'exit': exit_code, 'report': str(out.relative_to(ROOT)),
                      'queries_passed': sum(item.get('result') == '7' and item['seconds'] < 30 for item in report['queries']),
                      'error': report.get('error'), 'cleanup_errors': report.get('cleanup_errors'),
                      'evidence_complete': report['evidence_complete'], 'evidence_errors': report.get('evidence_errors'),
                      'container_absent': report['container_absent'], 'private_directory_removed': report['private_directory_removed'],
                      'lock_released': report.get('lock_released', False), 'port_closed': report.get('port_closed', False)}))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
