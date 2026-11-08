"""Independent fail-closed configuration and secret handling regression tests."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from smart_albums.config import ConfigError, load_api_key, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'config.yaml'
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.base = {'server_url': 'https://immich.example.test/api/', 'jobs': [
            {'name': 'Family', 'album': 'Family', 'include': {'people_any': ['Alice']}}]}

    def load(self, cfg=None):
        self.path.write_text(yaml.safe_dump(self.base if cfg is None else cfg), encoding='utf-8')
        return load_config(self.path)

    def test_safe_defaults_and_url_normalization(self):
        cfg = self.load()
        self.assertIs(cfg['dry_run'], True)
        self.assertEqual(cfg['server_url'], 'https://immich.example.test')
        self.assertEqual(cfg['timezone'], 'Asia/Shanghai')
        self.assertIs(cfg['jobs'][0]['enabled'], True)

    def test_duplicate_yaml_keys_rejected_at_every_level(self):
        for text in ['server_url: one\nserver_url: two\n',
                     'jobs:\n- name: one\n  name: two\n',
                     'include:\n  people_any: [Alice]\n  people_any: [Bob]\n']:
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_yaml_scalars_and_unsafe_tags_rejected(self):
        for raw in ["", "null", "[]", "true", "!!python/object/apply:os.system [echo unsafe]"]:
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_invalid_config_fields(self):
        for field, value in [('dry_run', 'false'), ('dry_run', 0),
                             ('run_on_startup', None), ('request_timeout', True),
                             ('request_timeout', 0), ('max_pages', 0),
                             ('max_pages', 2.5), ('timezone', 'Not/AZone'),
                             ('schedule', '* * * * * *'), ('schedule', '99 * * * *'),
                             ('schedule', '@daily'), ('jobs', []), ('typo', True)]:
            with self.subTest(field=field, value=value):
                cfg = copy.deepcopy(self.base)
                cfg[field] = value
                with self.assertRaises(ConfigError):
                    self.load(cfg)

    def test_bad_job_fields(self):
        for field, value in [('include', None), ('include', {}),
                             ('include', {'all_assets': False}),
                             ('include', {'local_all': [{'path': '$.id'}]}),
                             ('include', {'people_any': []}),
                             ('include', {'people_any': [12]}),
                             ('include', {'people_an': ['Alice']}),
                             ('include', {'smart_any': [{'query': 'sunset', 'limit': True}]}),
                             ('include', {'local_any': [{'path': '['}], 'all_assets': True}),
                             ('include', {'metadata_any': [{'page': 1}]}),
                             ('exclude', {'all_assets': True}),
                             ('exclude', {'local_any': [{'path': '$.id', 'regex': '['}]}),
                             ('enabled', 'true'), ('dry_run', 'false'),
                             ('max_matches', True), ('album', ''), ('unknown', 1)]:
            with self.subTest(field=field, value=value):
                cfg = copy.deepcopy(self.base)
                cfg['jobs'][0][field] = value
                with self.assertRaises(ConfigError):
                    self.load(cfg)

    def test_explicit_all_assets_allowed(self):
        self.base['jobs'][0]['include'] = {'all_assets': True}
        self.assertTrue(self.load()['jobs'][0]['include']['all_assets'])

    def test_metadata_unknown_null_and_invalid_types_fail_closed(self):
        cases = [{'isFavorit': True}, {'city': None}, {'id': None},
                 {'takenAfter': None}, {'type': []}, {'visibility': {}},
                 {'filter': {'isFavorit': {'eq': True}}},
                 {'filter': {'type': {'eq': []}}},
                 {'filter': {'city': {'typo': 'Paris'}}},
                 {'filter': {'city': {'eq': 'Paris'}}, 'orderBy': {'field': []}},
                 {'filter': {'city': {'eq': 'Paris'}},
                  'orderBy': {'field': 'createdAt', 'direction': []}}]
        for query in cases:
            with self.subTest(query=query):
                cfg = copy.deepcopy(self.base)
                cfg['jobs'][0]['include'] = {'metadata_any': [query]}
                with self.assertRaises(ConfigError):
                    self.load(cfg)

    def test_duplicate_job_names_rejected(self):
        self.base['jobs'].append(copy.deepcopy(self.base['jobs'][0]))
        with self.assertRaises(ConfigError):
            self.load()

    def test_unsafe_urls_rejected(self):
        for url in ['ftp://example.test', 'https://user:secret@example.test',
                    'https://example.test?token=secret', 'https://example.test#secret',
                    'http://[bad']:
            with self.subTest(url=url):
                self.base['server_url'] = url
                with self.assertRaises(ConfigError):
                    self.load()

    def test_invalid_utf8_config_is_config_error(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_api_key_env_precedes_secret_file(self):
        with patch.dict(os.environ, {'IMMICH_API_KEY': 'test-only-key'}):
            self.assertEqual(load_api_key({'api_key_file': '/nonexistent'}), 'test-only-key')

    def test_api_key_file_and_environment_path_override(self):
        secret = Path(self.directory.name) / 'test-secret'
        secret.write_text('test-only-key\n')
        with patch.dict(os.environ, {'IMMICH_API_KEY_FILE': str(secret)}):
            self.assertEqual(load_api_key({'api_key_file': '/nonexistent'}), 'test-only-key')
        self.assertEqual(load_api_key({'api_key_file': str(secret)}), 'test-only-key')

    def test_key_errors_do_not_include_secret(self):
        marker = 'test-only-secret with spaces'
        with patch.dict(os.environ, {'IMMICH_API_KEY': marker}):
            with self.assertRaises(ConfigError) as caught:
                load_api_key({})
        self.assertNotIn(marker, str(caught.exception))
        with self.assertRaises(ConfigError):
            load_api_key({'api_key_file': '/nonexistent'})

    def test_invalid_utf8_secret_is_config_error(self):
        secret = Path(self.directory.name) / 'test-secret'
        secret.write_bytes(b'\xff')
        with self.assertRaises(ConfigError):
            load_api_key({'api_key_file': str(secret)})


if __name__ == '__main__':
    unittest.main()
