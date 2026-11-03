from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('discovery_review_subject', ROOT / 'scripts/discover-fleet-host.py')
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)


def observed(text, ok=True):
    return {'ok': ok, 'returncode': 0 if ok else 1, 'output': text,
            'summary': text.strip(), 'output_hash': discovery.digest(text)}


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.object(discovery.Path, 'home', return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_git_fsmonitor_hook_is_not_executed(self):
        repo = self.root / 'repo'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git('init', '-q')
        (repo / 'tracked').write_text('original')
        git('add', 'tracked')
        hook = repo / 'monitor'
        marker = repo / 'executed'
        hook.write_text('#!/bin/sh\nprintf invoked > "' + str(marker) + '"\nprintf "token\\000"\n')
        hook.chmod(0o700)
        git('config', 'core.fsmonitor', str(hook))
        git('status', '--porcelain=v1', '--untracked-files=no')
        self.assertTrue(marker.exists(), 'positive control: installed hook must actually run')
        marker.unlink()
        discovery.repo_fact(repo)
        self.assertFalse(marker.exists(), 'discovery executed repository-owned code')

    def test_repository_change_invalidates_combined_observation(self):
        repo = self.root / 'repo'
        (repo / '.git').mkdir(parents=True)
        calls = 0
        def run(args, **kwargs):
            nonlocal calls
            if 'rev-parse' in args:
                calls += 1
                return observed(('a' if calls == 1 else 'b') * 40)
            if 'branch' in args: return observed('main')
            if 'remote' in args: return observed('https://github.com/clpi/idol.git')
            return observed('')
        with mock.patch.object(discovery, 'run', side_effect=run):
            fact = discovery.repo_fact(repo)
        self.assertIs(fact.get('observation_stable'), False)
        self.assertIsNone(fact['head'])
        self.assertIsNone(fact['origin_repository'])

    def test_null_self_is_not_complete_tailnet_inventory(self):
        raw = {'BackendState': 'Running', 'Self': None, 'Peer': {}}
        with mock.patch.object(discovery.shutil, 'which', return_value='tailscale'), \
             mock.patch.object(discovery, 'run', side_effect=[observed('', False), observed(json.dumps(raw))]):
            fact = discovery.tailscale_fact()
        self.assertFalse(fact['status_ok'])
        self.assertNotIn('peers', fact)

    def hermes(self, raw):
        path = self.root / '.hermes/config.yaml'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(raw))
        return discovery.hermes_fact()

    def test_malformed_hermes_field_types_refuse_without_echo(self):
        for raw in ({'providers': []}, {'mcp_servers': 2}, {'model': []},
                    {'model': {'provider': ['PRIVATE']}}, {'fallback_providers': ['PRIVATE']},
                    {'auxiliary': {'vision': 'PRIVATE'}}, {'delegation': False}):
            with self.subTest(raw=raw):
                fact = self.hermes(raw)
                self.assertFalse(fact['ok'])
                self.assertNotIn('PRIVATE', json.dumps(fact))

    def test_hermes_oversize_is_not_silently_truncated(self):
        fact = self.hermes({'mcp_servers': {str(i): {} for i in range(discovery.MAX_RECORDS + 1)}})
        self.assertFalse(fact['ok'])

    def test_missing_yaml_has_explicit_dependency_reason(self):
        path = self.root / '.hermes/config.yaml'
        path.parent.mkdir()
        path.write_text('{}')
        with mock.patch.dict('sys.modules', {'yaml': None}):
            fact = discovery.hermes_fact()
        self.assertEqual(fact['reason'], 'yaml-dependency-unavailable')

    def test_optional_tools_presence_does_not_execute_unknown_flags(self):
        for name in ('grok', 'poolside', 'agy', 'omp', 'oh-my-pi'):
            with self.subTest(name=name), mock.patch.object(discovery.shutil, 'which', return_value='/fixture/' + name), \
                 mock.patch.object(discovery, 'run', side_effect=AssertionError('unverified CLI invocation')):
                self.assertIn(name, discovery.COMMANDS)
                fact = discovery.command_fact(name)
                self.assertTrue(fact['found'])
                self.assertFalse(fact['version_ok'])

    def test_storage_permission_race_is_unknown_not_present(self):
        drive = self.root / 'drive'
        drive.mkdir()
        with mock.patch.object(discovery.shutil, 'disk_usage', side_effect=PermissionError):
            rows = discovery.storage_fact([drive])
        row = next(row for row in rows if row['path'] == '~/drive')
        self.assertIsNone(row['exists'])
        self.assertNotIn('capacity_bytes', row)

    def test_storage_disappearance_is_absent(self):
        drive = self.root / 'drive'
        drive.mkdir()
        with mock.patch.object(discovery.shutil, 'disk_usage', side_effect=FileNotFoundError):
            rows = discovery.storage_fact([drive])
        row = next(row for row in rows if row['path'] == '~/drive')
        self.assertIs(row['exists'], False)

    def receipt(self, catalog):
        args = SimpleNamespace(controller_root=self.root, repository=[], storage_path=[],
                               output=self.root/'details.json', receipt=self.root/'receipt.json')
        patches = {'parser': mock.Mock(parse_args=lambda: args), 'find_repositories': [],
                   'command_fact': {'found': False}, 'run': observed('a'*40),
                   'hardware_fact': {}, 'claim_fact': {}, 'ollama_fact': catalog,
                   'launchd_fact': {}, 'tailscale_fact': {}, 'storage_fact': [],
                   'hermes_fact': {'ok': False}}
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(discovery, name, return_value=value))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(discovery.main(), 0)
        return args, json.loads(args.receipt.read_text())

    def test_missing_models_do_not_hash_a_valid_empty_catalog(self):
        _, receipt = self.receipt({'ok': False})
        self.assertIsNone(receipt['ollama']['models_hash'])
        self.assertIsNone(receipt['ollama'].get('inventory_hash'))

    def test_same_model_name_changed_digest_changes_receipt(self):
        catalog = {'ok': True, 'models': ['model'], 'inventory': [{'name': 'model', 'digest': 'a'*64, 'size_bytes': 12}]}
        _, first = self.receipt(catalog)
        catalog['inventory'][0]['digest'] = 'b'*64
        _, second = self.receipt(catalog)
        self.assertIsInstance(first['ollama'].get('inventory_hash'), str)
        self.assertNotEqual(first['ollama']['inventory_hash'], second['ollama']['inventory_hash'])

    def test_receipt_binds_exact_detail_bytes_and_detects_replacement(self):
        args, receipt = self.receipt({'ok': False})
        expected = hashlib.sha256(args.output.read_bytes()).hexdigest()
        self.assertEqual(receipt.get('observation_sha256'), expected)
        args.output.write_text('{"different":true}\n')
        self.assertNotEqual(receipt['observation_sha256'], hashlib.sha256(args.output.read_bytes()).hexdigest())

    def test_catalog_hash_is_independent_of_server_order(self):
        rows = [{'name': 'one', 'digest': 'a'*64, 'size_bytes': 1}, {'name': 'two', 'digest': 'b'*64, 'size_bytes': 2}]
        _, first = self.receipt({'ok': True, 'models': ['one', 'two'], 'inventory': rows})
        _, second = self.receipt({'ok': True, 'models': ['one', 'two'], 'inventory': rows[::-1]})
        self.assertIsInstance(first['ollama'].get('inventory_hash'), str)
        self.assertEqual(first['ollama']['inventory_hash'], second['ollama']['inventory_hash'])

if __name__ == '__main__':
    unittest.main()
