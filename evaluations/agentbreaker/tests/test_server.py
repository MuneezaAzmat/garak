"""HTTP boundary tests run the real agent and executor, using scripted model I/O."""

import concurrent.futures
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from agentbreaker_eval.server import make_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.trace_path = Path(self.tempdir.name) / "traces.jsonl"

        def completion(payload):
            last = payload["messages"][-1]
            if last["role"] == "tool":
                message = {"role": "assistant", "content": "Refund completed."}
            else:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "refund", "type": "function", "function": {
                        "name": "process_refund", "arguments": json.dumps({
                            "order_id": "ORD-101", "amount": 20, "reason": "request"})}}]}
            return {"choices": [{"message": message, "finish_reason":
                                 "tool_calls" if "tool_calls" in message else "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}

        self.server = make_server({"model": "test-model"}, "safe", self.trace_path,
                                  0, completion=completion)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tempdir.cleanup()

    def post(self, data):
        request = urllib.request.Request(self.base_url + "/v1/chat/completions",
            data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response), response.headers
        except urllib.error.HTTPError as error:
            return error.code, json.load(error), error.headers

    def test_response_trace_id_matches_persisted_real_ledger_trace(self):
        status, response, headers = self.post({"model": "miniklarna-safe",
            "messages": [{"role": "user", "content": "Refund 20 on ORD-101."}]})
        self.assertEqual(status, 200)
        self.assertEqual(response["choices"][0]["message"]["content"], "Refund completed.")
        trace = json.loads(self.trace_path.read_text())
        self.assertEqual(response["metadata"]["trace_id"], trace["trace_id"])
        self.assertEqual(headers["x-agentbreaker-trace-id"], trace["trace_id"])
        self.assertEqual(trace["prompt"], "Refund 20 on ORD-101.")
        self.assertEqual(trace["after"]["orders"]["ORD-101"]["remaining_to_pay"], 20)
        self.assertEqual(response["usage"]["total_tokens"], 10)

    def test_concurrent_requests_have_independent_state_and_valid_jsonl(self):
        data = {"messages": [{"role": "user", "content": "Refund 20 on ORD-101."}]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.post(data), range(8)))
        self.assertTrue(all(status == 200 for status, _, _ in responses))
        traces = [json.loads(line) for line in self.trace_path.read_text().splitlines()]
        self.assertEqual(len(traces), 8)
        self.assertEqual(len({trace["trace_id"] for trace in traces}), 8)
        self.assertEqual(len({trace["before"]["session_id"] for trace in traces}), 8)
        self.assertTrue(all(trace["after"]["orders"]["ORD-101"]["remaining_to_pay"] == 20
                            for trace in traces))

    def test_streaming_and_multiple_choices_rejected_before_execution(self):
        for extra in ({"stream": True}, {"n": 2}):
            status, response, _ = self.post({"messages": [{"role": "user", "content": "hi"}], **extra})
            self.assertEqual(status, 400)
            self.assertEqual(response["error"]["type"], "invalid_request_error")
        self.assertFalse(self.trace_path.exists())

    def test_models_and_health_only_advertise_local_target(self):
        with urllib.request.urlopen(self.base_url + "/v1/models", timeout=3) as response:
            body = json.load(response)
        self.assertEqual(body["data"][0]["id"], "miniklarna-safe")
        with urllib.request.urlopen(self.base_url + "/health", timeout=3) as response:
            self.assertEqual(json.load(response)["status"], "ok")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_non_text_content_is_rejected_instead_of_discarded(self):
        status, response, _ = self.post({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.test/image"}}]}]})
        self.assertEqual(status, 400)
        self.assertIn("text", response["error"]["message"])

    def test_budget_rejection_is_persisted_as_unknown_operational_error(self):
        self.server.max_requests = 1
        data = {"messages": [{"role": "user", "content": "refund"}]}
        self.assertEqual(self.post(data)[0], 200)
        status, response, _ = self.post(data)
        self.assertEqual(status, 400)
        traces = [json.loads(line) for line in self.trace_path.read_text().splitlines()]
        self.assertEqual(len(traces), 2)
        self.assertEqual(traces[1]["error"]["type"], "request_budget_exhausted")
        self.assertIsNone(traces[1]["oracle"]["executed_policy_violations"])
        self.assertEqual(response["error"]["metadata"]["trace_id"], traces[1]["trace_id"])

    def test_provider_failure_returns_http_error_and_keeps_trace(self):
        def fail(_):
            raise RuntimeError("upstream connection failed")
        self.server.completion = fail
        status, response, _ = self.post({"messages": [{"role": "user", "content": "help"}]})
        self.assertEqual(status, 502)
        trace = json.loads(self.trace_path.read_text())
        self.assertEqual(trace["error"]["type"], "provider_error")
        self.assertEqual(response["error"]["metadata"]["trace_id"], trace["trace_id"])

    def test_budget_trace_redacts_configured_key_if_request_echoes_it(self):
        self.server.max_requests = 1
        self.post({"messages": [{"role": "user", "content": "help"}]})
        with patch.dict(os.environ, {"LITELLM_API_KEY": "test-secret-token"}):
            self.post({"messages": [{"role": "user", "content": "test-secret-token"}]})
        self.assertNotIn("test-secret-token", self.trace_path.read_text())

    def test_close_waits_for_inflight_execution_evidence(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = self.server.completion
        def slow(payload):
            entered.set()
            release.wait(5)
            return original(payload)
        self.server.completion = slow
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            response = pool.submit(self.post, {"messages": [{"role": "user", "content": "refund"}]})
            self.assertTrue(entered.wait(3))
            self.server.shutdown()
            def close():
                self.server.server_close()
                closed.set()
            closing = pool.submit(close)
            try:
                self.assertFalse(closed.wait(0.05), 'Close discarded an in-flight execution')
            finally:
                release.set()
            closing.result(timeout=3)
            self.assertEqual(response.result(timeout=3)[0], 200)
        self.assertEqual(json.loads(self.trace_path.read_text())["after"]["orders"]["ORD-101"]["remaining_to_pay"], 20)


if __name__ == "__main__":
    unittest.main()
