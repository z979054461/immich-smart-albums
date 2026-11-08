"""End-to-end CLI checks against an isolated local fake HTTP API, never a NAS."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from smart_albums.service import next_run

ALBUM = '10000000-0000-4000-8000-000000000001'
PERSON = '10000000-0000-4000-8000-000000000002'
A = '10000000-0000-4000-8000-000000000003'
B = '10000000-0000-4000-8000-000000000004'
KEY = 'fake-integration-key-never-real'
ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get('x-api-key') != KEY:
            self.answer({'message': 'bad key'}, 401)
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == '/api/albums':
            self.answer([{'id': ALBUM, 'albumName': '家人'}])
        elif self.path.startswith('/api/people?'):
            self.answer({'people': [{'id': PERSON, 'name': '爸爸'}], 'hasNextPage': False})
        else:
            self.answer({}, 404)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))

    def do_POST(self):
        if not self.authorized():
            return
        payload = self.body()
        self.server.searches.append(payload)
        if self.server.fail_search:
            self.answer({'message': KEY}, 403)
            return
        self.answer({'assets': {'items': [{'id': A if 'cursor' not in payload else B}],
                                'nextCursor': 'second' if 'cursor' not in payload else None,
                                'nextPage': None}})

    def do_PUT(self):
        if not self.authorized():
            return
        ids = self.body()['ids']
        results = []
        for aid in ids:
            duplicate = aid in self.server.existing
            results.append({'id': aid, 'success': not duplicate, **({'error': 'duplicate'} if duplicate else {})})
            self.server.existing.add(aid)
        self.server.writes.append(ids)
        self.answer(results)


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.writes = []
        self.server.searches = []
        self.server.existing = set()
        self.server.fail_search = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cfg = {'server_url': f'http://127.0.0.1:{self.server.server_port}',
                    'api_key_file': str(self.root / 'key'), 'dry_run': True,
                    'schedule': '0 2 * * *', 'timezone': 'Asia/Shanghai',
                    'jobs': [{'name': 'family', 'album': '家人', 'include': {'people_any': ['爸爸']}}]}
        (self.root / 'key').write_text(KEY)
        self.env = {**os.environ, 'SMART_ALBUMS_CONFIG': str(self.root / 'config.yaml'),
                    'SMART_ALBUMS_STATE_DIR': str(self.root / 'data')}
        self.env.pop('IMMICH_API_KEY', None)
        self.env.pop('IMMICH_API_KEY_FILE', None)
        self.env.pop('IMMICH_SERVER_URL', None)
        # Preserve dependency path used by the test runner, including a local target install.
        self.env['PYTHONPATH'] = os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)
        self.save()

    def save(self):
        (self.root / 'config.yaml').write_text(yaml.safe_dump(self.cfg), encoding='utf-8')

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'smart_albums', *args], cwd=ROOT, env=self.env,
                              text=True, capture_output=True, timeout=15)

    def assert_local_log_times(self, output):
        entries = [json.loads(line) for line in output.splitlines()]
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(datetime.fromisoformat(entry['time']).utcoffset(), timedelta(hours=8))
        return entries

    def test_preview_then_apply_then_idempotent_repeat(self):
        result = self.cli('once')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.server.writes, [])
        self.assertIn('"matched": 2', result.stdout)
        self.assert_local_log_times(result.stdout)
        self.cfg['dry_run'] = False
        self.save()
        result = self.cli('once')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(set(self.server.writes[0]), {A, B})
        self.assertIn('"added": 2', result.stdout)
        again = self.cli('once')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn('"duplicate": 2', again.stdout)
        self.assertNotIn(KEY, result.stdout + result.stderr + again.stdout + again.stderr)
        self.assertNotIn(KEY, (self.root / 'data' / 'last-run.json').read_text())

    def test_failed_read_is_nonzero_no_put_or_secret_leak(self):
        self.cfg['dry_run'] = False
        self.save()
        self.server.fail_search = True
        result = self.cli('once')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.server.writes, [])
        self.assertIn('HTTP 403', result.stdout)
        self.assert_local_log_times(result.stdout)
        self.assertNotIn(KEY, result.stdout + result.stderr)

    def test_check_and_help_do_not_require_key(self):
        (self.root / 'key').unlink()
        self.assertEqual(self.cli('check').returncode, 0)
        self.assertEqual(self.cli('--help').returncode, 0)
        result = self.cli('once')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.server.writes, [])

    def test_scheduler_heartbeat_and_sigterm(self):
        proc = subprocess.Popen([sys.executable, '-m', 'smart_albums'], cwd=ROOT, env=self.env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (self.root / 'data' / 'last-run.json').exists():
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNone(proc.poll())
            self.assertEqual(self.cli('healthcheck').returncode, 0)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stdout + stderr)
            self.assertIn('scheduler_stopped', stdout)
            entries = self.assert_local_log_times(stdout)
            self.assertEqual(entries[0]['event'], 'scheduler_started')
            self.assertEqual(entries[-1]['event'], 'scheduler_stopped')
            for entry in entries:
                if entry['event'] == 'next_run':
                    self.assertEqual(datetime.fromisoformat(entry['at']).utcoffset(), timedelta(hours=8))
            self.assertNotIn(KEY, stdout + stderr)
            self.assertEqual(self.cli('healthcheck').returncode, 1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_cron_uses_beijing_timezone(self):
        now = datetime(2026, 11, 8, 1, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(next_run('0 2 * * *', now).isoformat(), '2026-11-08T02:00:00+08:00')


if __name__ == '__main__':
    unittest.main()
