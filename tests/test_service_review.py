"""Offline, independent adversarial checks of service read/write boundaries."""
import contextlib
import io
import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from smart_albums.api import APIError, ImmichClient
from smart_albums.engine import run_job, select_assets
from smart_albums.service import AlreadyRunning, healthcheck, run_cycle, run_lock

A = '00000000-0000-4000-8000-000000000001'
B = '00000000-0000-4000-8000-000000000002'
C = '00000000-0000-4000-8000-000000000003'
KEY = 'test-only-secret-marker'


def page(ids, **tokens):
    return {'assets': {'items': [{'id': i} for i in ids], **tokens}}


def job():
    return {'name': 'test', 'album': 'target', 'enabled': True,
            'include': {'metadata_any': [{'isFavorite': True}]}}


class ClientSafetyTests(unittest.TestCase):
    def client(self, replies):
        client = ImmichClient('https://immich.invalid', KEY)
        client.request = Mock(side_effect=replies)
        self.addCleanup(client.close)
        return client

    def test_ambiguous_names_rejected(self):
        client = self.client([])
        client._people = [{'id': A, 'name': 'Alice'}, {'id': B, 'name': 'Alice'}]
        client._albums = [{'id': A, 'albumName': 'target'}, {'id': B, 'albumName': 'target'}]
        for resolve, name in [(client.resolve_person, 'Alice'), (client.resolve_album, 'target')]:
            with self.subTest(name=name), self.assertRaises(APIError):
                resolve(name)
        self.assertEqual(client.resolve_person(A), A)
        self.assertEqual(client.resolve_album(B), B)

    def test_read_failure_or_truncation_never_writes(self):
        cases = [[page([A])], [page([A], nextPage='2'), APIError('read failed')],
                 [page([A], nextPage='2'), page([A], nextPage=None)],
                 [page([A], nextPage='1')], [page([A], nextPage=2)],
                 [page([A], nextPage='2'), page([], nextPage='3')]]
        for replies in cases:
            with self.subTest(replies=replies):
                client = self.client(replies)
                client._albums = [{'id': C, 'albumName': 'target'}]
                client.add_assets = Mock()
                with self.assertRaises(APIError):
                    run_job(client, job())
                client.add_assets.assert_not_called()

    def test_max_page_limit_never_returns_partial(self):
        client = self.client([page([A], nextPage='2')])
        client.max_pages = 1
        with self.assertRaises(APIError):
            client.search({'isFavorite': True})

    def test_cursor_pages_consumed_before_write(self):
        client = self.client([page([A], nextCursor='opaque-token'), page([B], nextCursor=None)])
        self.assertEqual([r['id'] for r in client.search({'filter': {'isFavorite': True}})], [A, B])
        self.assertEqual(client.request.call_args_list[1].kwargs['payload']['cursor'], 'opaque-token')

    def test_dry_run_reads_but_never_puts(self):
        client = self.client([page([A], nextPage=None)])
        client._albums = [{'id': C, 'albumName': 'target'}]
        client.add_assets = Mock()
        self.assertEqual(run_job(client, job(), dry_run=True)['matched'], 1)
        client.add_assets.assert_not_called()

    def test_explicit_write_failure_is_not_success(self):
        client = self.client([[{'id': A, 'success': False, 'error': 'forbidden'},
                               {'id': B, 'success': False, 'error': 'duplicate'},
                               {'id': C, 'success': True}]])
        self.assertEqual(client.add_assets(C, [A, B, C]), {'added': 1, 'duplicate': 1, 'failed': 1})

    def test_wrong_or_missing_write_ids_are_uncertain(self):
        for reply in [[], [{'id': B, 'success': True}], {'success': True}]:
            with self.subTest(reply=reply):
                with self.assertRaises(APIError):
                    self.client([reply]).add_assets(C, [A])

    def test_redirect_refused_without_forwarding_secret(self):
        session = Mock()
        session.headers = {}
        session.request.return_value.status_code = 302
        client = ImmichClient('https://immich.invalid', KEY, session=session)
        with self.assertRaises(APIError) as caught:
            client.request('GET', '/albums')
        self.assertNotIn(KEY, str(caught.exception))
        session.request.assert_called_once()
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        self.assertFalse(session.trust_env)

    def test_write_network_error_not_retried_or_leaked(self):
        session = Mock()
        session.headers = {}
        session.request.side_effect = requests.ConnectionError(KEY)
        client = ImmichClient('https://immich.invalid', KEY, session=session)
        with self.assertRaises(APIError) as caught:
            client.request('PUT', '/albums/' + C + '/assets', payload={'ids': [A]})
        session.request.assert_called_once()
        self.assertIn('uncertain', str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_exclusion_read_failure_prevents_write(self):
        client = Mock()
        client.resolve_album.return_value = C
        client.search.side_effect = [[{'id': A}], APIError('exclusion failed')]
        task = job()
        task['exclude'] = {'metadata_any': [{'isArchived': True}]}
        with self.assertRaises(APIError):
            run_job(client, task)
        client.add_assets.assert_not_called()

    def test_include_groups_intersect_exclusion_groups_union(self):
        client = Mock()
        client.resolve_person.side_effect = [A, B]
        client.search.side_effect = [[{'id': A}, {'id': B}], [{'id': B}, {'id': C}],
                                    [{'id': B}, {'id': C}], [{'id': C}]]
        task = job()
        task['include'] = {'people_any': ['Alice', 'Bob'],
                           'metadata_all': [{'isFavorite': True}]}
        task['exclude'] = {'metadata_any': [{'visibility': 'archive'}]}
        self.assertEqual(select_assets(client, task), [B])

    def test_max_matches_guard_prevents_write(self):
        client = Mock()
        client.resolve_album.return_value = C
        client.search.return_value = [{'id': A}, {'id': B}]
        task = job()
        task['max_matches'] = 1
        with self.assertRaises(APIError):
            run_job(client, task)
        client.add_assets.assert_not_called()


class ServiceSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_overlapping_lock_rejected_and_released(self):
        with run_lock(self.directory):
            with self.assertRaises(AlreadyRunning):
                with run_lock(self.directory):
                    self.fail('Overlapping lock acquired')
        with run_lock(self.directory):
            pass

    def test_all_dry_run_precedence_combinations(self):
        for global_dry, job_dry, force_dry in itertools.product((False, True), repeat=3):
            with self.subTest(global_dry=global_dry, job_dry=job_dry, force_dry=force_dry):
                task = job()
                task['dry_run'] = job_dry
                cfg = {'server_url': 'https://immich.invalid', 'request_timeout': 5,
                       'max_pages': 2, 'dry_run': global_dry, 'jobs': [task]}
                with patch('smart_albums.service.load_api_key', return_value=KEY), \
                     patch('smart_albums.service.ImmichClient'), \
                     patch('smart_albums.service.run_job', return_value={'status': 'ok'}) as run, \
                     contextlib.redirect_stdout(io.StringIO()):
                    run_cycle(cfg, self.directory, force_dry_run=force_dry)
                self.assertEqual(run.call_args.kwargs['dry_run'], global_dry or job_dry or force_dry)

    def test_unexpected_exception_is_redacted_in_log_and_state(self):
        cfg = {'server_url': 'https://immich.invalid', 'request_timeout': 5,
               'max_pages': 2, 'dry_run': True, 'jobs': [job()]}
        output = io.StringIO()
        with patch('smart_albums.service.load_api_key', return_value=KEY), \
             patch('smart_albums.service.ImmichClient'), \
             patch('smart_albums.service.run_job', side_effect=RuntimeError(KEY)), \
             contextlib.redirect_stdout(output):
            result = run_cycle(cfg, self.directory)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(KEY, output.getvalue())
        self.assertNotIn(KEY, (self.directory / 'last-run.json').read_text())

    def test_health_rejects_non_object_state(self):
        heart = self.directory / 'heartbeat.json'
        last = self.directory / 'last-run.json'
        for raw in ['[]', 'null', 'true', '42', '"text"']:
            with self.subTest(raw=raw):
                heart.write_text(raw)
                self.assertFalse(healthcheck(self.directory))
                heart.write_text(json.dumps({'timestamp': time.time()}))
                last.write_text(raw)
                self.assertFalse(healthcheck(self.directory))
                last.unlink()

    def test_health_requires_recent_heartbeat_and_successful_last_run(self):
        self.assertFalse(healthcheck(self.directory))
        heart = self.directory / 'heartbeat.json'
        heart.write_text(json.dumps({'timestamp': time.time()}))
        self.assertTrue(healthcheck(self.directory))
        (self.directory / 'last-run.json').write_text(json.dumps({'status': 'failed'}))
        self.assertFalse(healthcheck(self.directory))
        (self.directory / 'last-run.json').write_text(json.dumps({'status': 'ok'}))
        heart.write_text(json.dumps({'timestamp': time.time() - 120}))
        self.assertFalse(healthcheck(self.directory))


if __name__ == '__main__':
    unittest.main()
