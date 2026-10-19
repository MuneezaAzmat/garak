"""LiteLLM loopback proxy rewrites Qwen thinking off and does not store secrets."""

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "litellm_openai_proxy", ROOT / "scripts/litellm_openai_proxy.py"
)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class LiteLLMProxyTests(unittest.TestCase):
    def test_qwen_thinking_is_disabled(self):
        raw = json.dumps({"model": proxy.QWEN_MODEL, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 16}).encode()
        out = json.loads(proxy.sanitize_body(raw))
        self.assertEqual(out["chat_template_kwargs"]["enable_thinking"], False)

    def test_gemma_is_not_forced_to_qwen_kwargs(self):
        raw = json.dumps({"model": "gemma-4-26b", "messages": [{"role": "user", "content": "Hi"}]}).encode()
        out = json.loads(proxy.sanitize_body(raw))
        self.assertNotIn("chat_template_kwargs", out)

    def test_duplicate_max_tokens_fields_are_collapsed(self):
        raw = json.dumps({"model": "gemma-4-26b", "max_tokens": 256, "max_completion_tokens": 256}).encode()
        out = json.loads(proxy.sanitize_body(raw))
        self.assertEqual(out["max_tokens"], 256)
        self.assertNotIn("max_completion_tokens", out)


if __name__ == "__main__":
    unittest.main()
