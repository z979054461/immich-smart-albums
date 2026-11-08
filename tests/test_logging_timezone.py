"""Timezone display regressions, without touching a real Immich server."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from smart_albums.__main__ import main
from smart_albums.config import ConfigError, load_config
from smart_albums.service import AlreadyRunning, log, run_cycle, serve

INSTANT = datetime(2026, 11, 8, 20, 18, 50, 28238, tzinfo=timezone.utc)
KEY = 'fake-timezone-test-key'


class LogTimezoneTests(unittest.TestCase):
    def emit(self, **kwargs):
        output = io.StringIO()
        with patch('smart_albums.service.datetime') as clock, contextlib.redirect_stdout(output):
            clock.now.side_effect = lambda tz: INSTANT.astimezone(tz)
            log('example', **kwargs)
        return json.loads(output.getvalue())

    def test_configured_zone_converts_time_and_date_not_just_suffix(self):
        entry = self.emit(tz=ZoneInfo('Asia/Shanghai'), timezone='Asia/Shanghai', job='家人')
        self.assertEqual(entry['time'], '2026-11-09T04:18:50.028238+08:00')
        self.assertEqual(entry['timezone'], 'Asia/Shanghai')
        self.assertEqual(entry['job'], '家人')
        self.assertEqual(datetime.fromisoformat(entry['time']), INSTANT)

    def test_default_is_safe_utc(self):
        self.assertEqual(self.emit()['time'], INSTANT.isoformat())

    def test_dst_zone_and_no_global_timezone_leak(self):
        for instant, offset in [(INSTANT, -5), (INSTANT.replace(month=7), -4)]:
            with self.subTest(instant=instant), patch(__name__ + '.INSTANT', instant):
                entry = self.emit(tz=ZoneInfo('America/New_York'))
                self.assertEqual(datetime.fromisoformat(entry['time']).utcoffset(), timedelta(hours=offset))
                self.assertEqual(self.emit()['time'], instant.isoformat())


class ConfiguredLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / 'config.yaml'
        self.key = self.directory / 'key'
        self.key.write_text(KEY)
        self.cfg = {'server_url': 'http://immich.example.test', 'timezone': 'Asia/Shanghai',
                    'api_key_file': str(self.key), 'schedule': '0 */6 * * *',
                    'jobs': [{'name': 'test', 'album': 'target',
                              'include': {'all_assets': True}}]}
        self.save()
        env = patch.dict(os.environ, {'TZ': 'Pacific/Honolulu',
                                     'SMART_ALBUMS_STATE_DIR': str(self.directory / 'state')}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        clock = patch('smart_albums.service.datetime')
        clock.start().now.side_effect = lambda tz: INSTANT.astimezone(tz)
        self.addCleanup(clock.stop)

    def save(self):
        self.path.write_text(yaml.safe_dump(self.cfg))

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(['--config', str(self.path), *args])
        return result, output.getvalue()

    def entries(self, text, offset=8):
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(rows)
        for row in rows:
            instant = datetime.fromisoformat(row['time'])
            self.assertEqual(instant.utcoffset(), timedelta(hours=offset), row)
            self.assertEqual(instant, INSTANT)
        return rows

    def test_check_uses_config_even_when_environment_tz_differs(self):
        status, text = self.cli('check')
        self.assertEqual(status, 0)
        self.assertEqual(self.entries(text)[0]['event'], 'config_valid')

    def test_default_config_timezone_is_shanghai(self):
        del self.cfg['timezone']
        self.save()
        status, text = self.cli('check')
        self.assertEqual(status, 0)
        self.entries(text)

    def test_non_shanghai_zone_and_repeated_main_do_not_leak(self):
        self.cfg['timezone'] = 'Asia/Tokyo'
        self.save()
        self.entries(self.cli('check')[1], 9)
        self.path.unlink()
        status, text = self.cli('check')
        self.assertEqual(status, 2)
        self.entries(text, 0)

    def test_invalid_timezone_or_configuration_falls_back_to_utc(self):
        for raw in ['[invalid yaml', 'timezone: Not/AZone\nserver_url: http://example.test\njobs: []',
                    yaml.safe_dump({**self.cfg, 'schedule': 'invalid'}),
                    yaml.safe_dump({**self.cfg, 'jobs': []})]:
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                status, text = self.cli('check')
                self.assertEqual(status, 2)
                self.assertEqual(self.entries(text, 0)[0]['event'], 'error')

    def test_missing_secret_errors_use_configured_zone_for_all_network_commands(self):
        self.key.unlink()
        for command in ['once', 'serve', 'list-people', 'list-albums']:
            with self.subTest(command=command):
                status, text = self.cli(command)
                self.assertEqual(status, 2)
                self.assertEqual(self.entries(text)[0]['event'], 'error')
                self.assertNotIn(KEY, text)

    def test_invalid_cli_job_uses_configured_zone(self):
        status, text = self.cli('once', '--job', 'missing')
        self.assertEqual(status, 2)
        self.entries(text)

    def test_unexpected_and_os_errors_use_configured_zone_and_redact_details(self):
        for exception in [OSError(KEY), RuntimeError(KEY)]:
            with self.subTest(exception=exception), patch('smart_albums.__main__.state_dir', side_effect=exception):
                status, text = self.cli('once')
            self.assertEqual(status, 2)
            self.entries(text)
            self.assertNotIn(KEY, text)

    def test_list_outputs_remain_plain_json_arrays(self):
        for command, method, name in [('list-people', 'people', 'name'), ('list-albums', 'albums', 'albumName')]:
            with self.subTest(command=command), patch('smart_albums.__main__.ImmichClient') as client:
                getattr(client.return_value, method).return_value = [{'id': 'test-id', name: '测试'}]
                status, text = self.cli(command)
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(text), [{'id': 'test-id', 'name': '测试'}])

    def test_healthcheck_help_and_version_need_no_config(self):
        self.path.unlink()
        with patch('smart_albums.__main__.load_config') as load:
            status, text = self.cli('healthcheck')
            self.assertEqual(status, 1)
            self.assertEqual(text, '')
            for arg in ['--help', '--version']:
                with self.subTest(arg=arg), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main([arg])
                self.assertEqual(caught.exception.code, 0)
            load.assert_not_called()

    def test_once_logs_use_local_time_but_state_remains_utc(self):
        cfg = load_config(self.path)
        output = io.StringIO()
        with patch('smart_albums.service.ImmichClient'), \
             patch('smart_albums.service.run_job', return_value={'status': 'ok'}), \
             contextlib.redirect_stdout(output):
            result = run_cycle(cfg, self.directory)
        self.assertEqual([row['event'] for row in self.entries(output.getvalue())], ['job_started', 'job_finished'])
        self.assertEqual(result['started_at'], INSTANT.isoformat())
        self.assertEqual(result['finished_at'], INSTANT.isoformat())
        self.assertEqual(json.loads((self.directory / 'last-run.json').read_text()), result)

    def test_scheduler_start_skip_and_stop_use_configured_zone(self):
        cfg = load_config(self.path)
        output = io.StringIO()
        with patch('smart_albums.service.run_cycle', side_effect=AlreadyRunning), \
             patch('smart_albums.service.next_run', side_effect=RuntimeError('end test')), \
             contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, 'end test'):
            serve(cfg, self.directory)
        events = [row['event'] for row in self.entries(output.getvalue())]
        self.assertEqual(events, ['scheduler_started', 'run_skipped', 'scheduler_stopped'])

    def test_scheduler_cycle_failure_and_stop_use_configured_zone(self):
        output = io.StringIO()
        with patch('smart_albums.service.run_cycle', side_effect=ConfigError('test')), \
             contextlib.redirect_stdout(output):
            self.assertEqual(serve(load_config(self.path), self.directory), 0)
        events = [row['event'] for row in self.entries(output.getvalue())]
        self.assertEqual(events, ['scheduler_started', 'run_failed', 'scheduler_stopped'])

    def test_heartbeat_thread_failure_uses_configured_zone(self):
        cfg = load_config(self.path)
        cfg['run_on_startup'] = False
        output = io.StringIO()
        # The main thread may calculate a tick before the heartbeat thread fails.
        # Avoid passing the mocked clock class into croniter in that interleaving.
        due = datetime.now(ZoneInfo('Asia/Shanghai')) + timedelta(days=1)
        with patch('smart_albums.service.write_state', side_effect=OSError), \
             patch('smart_albums.service.next_run', return_value=due), \
             contextlib.redirect_stdout(output):
            self.assertEqual(serve(cfg, self.directory), 0)
        events = [row['event'] for row in self.entries(output.getvalue())]
        self.assertIn('heartbeat_failed', events)
        self.assertIn('scheduler_started', events)
        self.assertEqual(events[-1], 'scheduler_stopped')


if __name__ == '__main__':
    unittest.main()
