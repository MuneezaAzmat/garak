"""No-socket test of the integration against the pinned native probe loop."""

import os
from pathlib import Path
import subprocess
import sys

from agentbreaker_eval.runner import worker_environment


def test_scenario_native_loop_config_first_attack_and_single_refinement(tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = worker_environment("0.17.0", tmp_path)
    script = r'''
import io
from pathlib import Path
import sys
from agentbreaker_eval.worker import _prepare
from agentbreaker_eval.asago_worker import scenario_probe_class
config = {"provider": {"base_url":"http://127.0.0.1:11434/v1", "model":"qwen2.5:14b", "require_api_key":False},
          "max_attempts_per_tool":2, "max_target_requests":2}
garak, base, _, _, upstream, cfg = _prepare(config, Path(sys.argv[1]))
from garak.attempt import Message
from garak.generators.base import Generator
from unittest.mock import patch
kind = scenario_probe_class(base)
cfg['probes']['asago_scenario'] = {kind.__name__: cfg['probes']['agent_breaker']['AgentBreaker']}
upstream.transient.reportfile = io.StringIO()
# Native construction probes URI reachability before our scripted target is
# installed. Stub only that HTTP boundary; this test deliberately uses no server.
with patch('httpx.get'):
    probe = kind(config_root=cfg)
assert probe.red_team_model_name == 'qwen2.5:14b'
assert probe.max_attempts_per_tool == 2
assert probe.soft_probe_prompt_cap == 2
probe.scenario_tool = 'process_refund'
probe.analysis_output = Path(sys.argv[1])
base._analyze_attackable_tools = lambda self: {'tool_analyses': {'process_refund': {'attack_prompts':['Seed request', 'Second initial request'], 'vulnerabilities':'Cross-owner refund'}}, 'priority_targets':['process_refund']}
probe._setup_red_team_model = lambda: None
probe._verify_attack_success = lambda **kwargs: (False, 0.9, 'Blocked')
probe._generate_exploit_prompt = lambda **kwargs: 'Refined request'
class Target(Generator):
    """Unit-test target, no model or tool execution."""
    __module__ = 'agentbreaker_eval.generators.scripted'
    parallel_capable = False
    def _call_model(self,prompt,generations_this_call=1):
        assert len(prompt.turns) == 1 and prompt.turns[0].role == 'user'
        self.requests.append(prompt.last_message('user').text)
        return [Message('Blocked')]
target=Target(name='scripted',config_root=cfg)
target.requests=[]
attempts=list(probe.probe(target))
assert len(attempts)==2, len(attempts)
assert len(target.requests)==2
assert 'Seed request' in target.requests[0]
assert 'Refined request' in target.requests[1]
import json
selection = json.loads((Path(sys.argv[1])/'initial-attack-selection.json').read_text())
assert selection['discarded_count'] == 1
assert all(a.notes['tool_description'] for a in attempts)
assert all(a.notes['verified_results']==[(False,0.9)] for a in attempts)
assert probe._generate_next_attempts.__func__ is base._generate_next_attempts
print('NATIVE_SEEDED_LOOP_OK')
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=root,
                            env=environment, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NATIVE_SEEDED_LOOP_OK" in result.stdout
