import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('discovery_parsing_subject', ROOT / 'scripts/discover-fleet-host.py')
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)


class MetadataParsing(unittest.TestCase):
    def test_nested_ollama_json_is_unavailable_not_exception(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'[' * 10000 + b'0' + b']' * 10000
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(discovery.http.client, 'HTTPConnection', return_value=connection):
            fact = discovery.ollama_fact()
        self.assertFalse(fact['ok'])
        self.assertNotIn('models', fact)
        connection.close.assert_called_once()

    def test_nested_tailscale_json_is_unavailable_not_exception(self):
        result = {'ok': True, 'output': '[' * 10000 + '0' + ']' * 10000, 'output_hash': 'fixture'}
        with mock.patch.object(discovery.shutil, 'which', return_value='tailscale'), \
             mock.patch.object(discovery, 'run', return_value=result):
            fact = discovery.tailscale_fact()
        self.assertFalse(fact['status_ok'])
        self.assertNotIn('peers', fact)

    def test_nested_adapter_json_is_unavailable(self):
        self.assertIsNone(discovery.json_object('[' * 10000 + '0' + ']' * 10000))

    def test_long_unmatched_metadata_finishes_in_bounded_process(self):
        code = '''import importlib.util, sys
spec=importlib.util.spec_from_file_location("subject", sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
assert module.clean("z"*40000, limit=32)=="z"*32
'''
        result = subprocess.run([sys.executable, '-I', '-c', code,
                                 str(ROOT / 'scripts/discover-fleet-host.py')],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_long_credential_keys_keep_complete_redaction(self):
        for key in ('prefix' * 200 + 'token' + 'suffix' * 200,
                    'prefix' * 200 + 'api_key' + 'suffix' * 200,
                    'PASSWORD' + 'suffix' * 200):
            for value in ('PRIVATEVALUE', '\"PRIVATE VALUE\"', "'PRIVATE VALUE'"):
                with self.subTest(key=key, value=value):
                    result = discovery.clean(key + '=' + value, limit=10000)
                    self.assertEqual(result, '[secret-redacted]')
        self.assertEqual(discovery.clean('ordinary=value'), 'ordinary=value')


if __name__ == '__main__':
    unittest.main()
