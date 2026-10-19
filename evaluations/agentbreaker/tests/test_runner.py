import json
import io
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from agentbreaker_eval.runner import load_env, load_config, worker_environment, join_attempts
from agentbreaker_eval import runner


class RunnerTests(unittest.TestCase):
    def test_ogx_manifest_preserves_native_runtime_and_summary_visibility(self):
        config = runner.load_config(runner.ROOT / 'configs/ollama-qwen.json')
        config['provider']['target_backend'] = 'ogx'
        with tempfile.TemporaryDirectory() as temporary:
            _, manifest = runner.initialize_run(temporary, config)
        notes = ' '.join(manifest['study_notes'])
        self.assertIn('OGX Responses', notes)
        self.assertIn('seven', notes)
        self.assertNotIn('Full-state summary is evaluator-only', notes)
        self.assertNotIn('not OGX/MCP transport validation', notes)

    def test_unauthenticated_local_doctor_does_not_require_or_forward_cloud_key(self):
        config = runner.load_config(runner.ROOT / 'configs/pilot.json')
        config['provider'].update(base_url='http://127.0.0.1:11434/v1', model='qwen2.5:14b',
                                  require_api_key=False, api_key_env='OLLAMA_API_KEY')
        replies = [io.BytesIO(b'{"data":[{"id":"qwen2.5:14b"}]}'),
                   io.BytesIO(b'{"choices":[{"message":{"content":"OK"}}]}')]
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'LITELLM_API_KEY': 'cloud-secret'}, clear=True):
            directory, _ = runner.initialize_run(temporary, config)
            with patch('agentbreaker_eval.runner.urlopen', side_effect=replies) as call:
                result = runner.doctor(directory, config, provider_only=True)
            self.assertTrue(result['ok'])
            self.assertEqual(call.call_count, 2)
            for args in call.call_args_list:
                self.assertFalse(args.args[0].has_header('Authorization'))

    def test_doctor_probes_a_separate_attacker_endpoint(self):
        config = runner.load_config(runner.ROOT / 'configs/pilot.json')
        config['provider'].update(base_url='http://127.0.0.1:11434/v1', model='qwen2.5:14b',
                                  require_api_key=False, api_key_env='OLLAMA_API_KEY')
        config['attacker'] = {'base_url': 'http://127.0.0.1:18790/v1', 'model': 'qwen-3.8-27b',
                              'require_api_key': False, 'api_key_env': 'LITELLM_API_KEY'}
        replies = [io.BytesIO(b'{"data":[{"id":"qwen2.5:14b"}]}'),
                   io.BytesIO(b'{"choices":[{"message":{"content":"OK"}}]}'),
                   io.BytesIO(b'{"data":[{"id":"qwen-3.8-27b"}]}'),
                   io.BytesIO(b'{"choices":[{"message":{"content":"OK"}}]}')]
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            directory, _ = runner.initialize_run(temporary, config)
            with patch('agentbreaker_eval.runner.urlopen', side_effect=replies) as call:
                result = runner.doctor(directory, config, provider_only=True)
            self.assertTrue(result['ok'])
            self.assertEqual(call.call_count, 4)
            urls = [args.args[0].full_url for args in call.call_args_list]
            self.assertTrue(any('11434' in url for url in urls))
            self.assertTrue(any('18790' in url for url in urls))

    def test_dotenv_does_not_evaluate_shell_or_overwrite_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('ABC_EVAL="literal$(command)"\nEXISTING=new\n')
            with patch.dict(os.environ, {'EXISTING': 'old'}, clear=True):
                load_env(path)
                self.assertEqual(os.environ['ABC_EVAL'], 'literal$(command)')
                self.assertEqual(os.environ['EXISTING'], 'old')

    def test_config_rejects_embedded_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'provider': {'api_key': 'not-a-real-key'}}))
            with self.assertRaisesRegex(ValueError, 'environment'):
                load_config(path)

    def test_version_worker_isolates_import_and_cache_paths(self):
        env = worker_environment('0.17.0', Path('/tmp/eval-worker'))
        self.assertIn('vendor/garak-0.17.0', env['PYTHONPATH'])
        self.assertNotIn('vendor/garak-0.16.0', env['PYTHONPATH'])
        self.assertEqual(env['XDG_CACHE_HOME'], str(Path('/tmp/eval-worker').resolve() / 'xdg/cache'))

    def test_unknown_version_cannot_choose_arbitrary_code_directory(self):
        with self.assertRaises(ValueError):
            worker_environment('../untrusted', Path('/tmp/eval-worker'))

    def test_duplicate_prompts_join_in_order_without_reusing_trace(self):
        traces = [dict(trace_id='one', prompt='p', response='r'), dict(trace_id='two', prompt='p', response='r')]
        attempts = [dict(prompt='p', response='r', score=0.0), dict(prompt='p', response='r', score=None)]
        joined = join_attempts(traces, attempts, '0.17.0')
        self.assertEqual([x['trace_id'] for x in joined], ['one', 'two'])
        self.assertIsNone(joined[1]['score'])
        self.assertIsNone(joined[1]['success'])

    def test_unmatched_native_result_is_explicit_error(self):
        joined = join_attempts([], [dict(prompt='p', response='r', score=1)], '0.17.0')
        self.assertIsNone(joined[0]['trace_id'])
        self.assertEqual(joined[0]['error'], 'unmatched_target_trace')

    def test_cli_controls_work_without_test_only_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.pop('PYTHONPATH', None)
            result = subprocess.run([sys.executable, '-m', 'agentbreaker_eval', 'controls', '--run-dir', directory],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((Path(directory) / 'manifest.json').read_text())
            self.assertEqual(manifest['stages']['controls']['count'], 10)
            self.assertEqual(manifest['stages']['controls']['failed_count'], 0)

    def test_failed_baseline_cannot_turn_into_completed_on_resume(self):
        config = runner.load_config(runner.ROOT / 'configs/pilot.json')
        with tempfile.TemporaryDirectory() as temporary:
            directory, _ = runner.initialize_run(temporary, config)
            runner.append_jsonl(directory / 'traces.jsonl', [{'kind': 'baseline', 'error': {'type': 'provider_error'}}])
            with patch('agentbreaker_eval.agent.run_agent') as call:
                runner.baseline(directory, config)
                call.assert_not_called()
            state = json.loads((directory / 'manifest.json').read_text())['stages']['baseline']
            self.assertEqual(state['status'], 'partial')

    def test_frozen_rejudge_resume_rejects_changed_input_set(self):
        config = runner.load_config(runner.ROOT / 'configs/pilot.json')
        with tempfile.TemporaryDirectory() as temporary:
            directory, _ = runner.initialize_run(temporary, config)
            runner.append_jsonl(directory / 'traces.jsonl', [{'trace_id': 'new', 'kind': 'baseline', 'response': 'reply', 'error': None}])
            for version in config['versions']:
                work = directory / 'rejudge' / version
                work.mkdir(parents=True)
                (work / 'result.json').write_text(json.dumps({'status': 'completed', 'input_sha256': 'old-input-set'}))
            with patch('agentbreaker_eval.runner.subprocess.run') as call:
                runner.rejudge(directory, config)
                call.assert_not_called()
            state = json.loads((directory / 'manifest.json').read_text())['stages']['rejudge']
            self.assertEqual(state['status'], 'partial')
            self.assertEqual(state['failed_versions'], config['versions'])

    def test_completed_worker_with_only_judge_errors_is_partial(self):
        config = runner.load_config(runner.ROOT / 'configs/pilot.json')
        with tempfile.TemporaryDirectory() as temporary:
            directory, _ = runner.initialize_run(temporary, config)
            runner.append_jsonl(directory / 'traces.jsonl', [{'trace_id': 'one', 'kind': 'baseline', 'response': 'reply', 'error': None}])
            def failed_judge(command, **kwargs):
                output = Path(command[command.index('--output') + 1])
                runner.append_jsonl(output / 'results.jsonl', [{'trace_id': 'one', 'score': None, 'error': 'judge_unavailable', 'phase': 'rejudge'}])
                return subprocess.CompletedProcess(command, 0)
            with patch('agentbreaker_eval.runner.subprocess.run', side_effect=failed_judge):
                runner.rejudge(directory, config)
            state = json.loads((directory / 'manifest.json').read_text())['stages']['rejudge']
            self.assertEqual(state['status'], 'partial')
            self.assertEqual(len(runner.read_jsonl(directory / 'judgements.jsonl')), 2)


if __name__ == '__main__':
    unittest.main()
