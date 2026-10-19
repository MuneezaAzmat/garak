"""Offline fixtures matching installed OGX 1.4 Responses schemas, not live runs."""

import copy
import json
import unittest

from agentbreaker_eval.ogx_trace import parse_response


REFUND_ARGUMENTS = '{"order_id":"ORD-101","amount":20,"reason":"customer request"}'
REFUND_RESULT = {"refund_id": "refund-example", "order_id": "ORD-101",
                 "amount": 20, "status": "COMPLETED", "remaining_to_pay": 20}


def mcp_call(**changes):
    # OGX 1.4 MCP output items have no status attribute.
    return {"type": "mcp_call", "id": "mcp-example", "server_label": "klarna-safe",
            "name": "process_refund", "arguments": REFUND_ARGUMENTS,
            "output": json.dumps(REFUND_RESULT), "error": None, **changes}


def message(text, **changes):
    return {"id": "msg-example", "type": "message", "role": "assistant",
            "status": "completed", "content": [
                {"type": "output_text", "text": text, "annotations": []}], **changes}


def response(*items, **changes):
    return {"id": "resp-example", "object": "response", "created_at": 0,
            "model": "openai/qwen2.5:14b", "status": "completed",
            "error": None, "incomplete_details": None, "store": True,
            "output": list(items), "usage": {
                "input_tokens": 750, "output_tokens": 80, "total_tokens": 830,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens_details": {"reasoning_tokens": 0}}, **changes}


class OGXTraceTests(unittest.TestCase):
    def test_real_mcp_shape_preserves_arguments_result_catalog_and_usage(self):
        catalog = {"type": "mcp_list_tools", "id": "list-example",
                   "server_label": "klarna-safe", "tools": [
                       {"name": "process_refund", "description": "Refund order",
                        "input_schema": {"type": "object"}}]}
        payload = response(catalog, mcp_call(), message("Refund completed."))
        original = copy.deepcopy(payload)
        parsed = parse_response(payload)
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["response"], "Refund completed.")
        self.assertEqual(len(parsed["calls"]), 1)
        call = parsed["calls"][0]
        self.assertEqual(call["name"], "process_refund")
        self.assertEqual(call["arguments"], json.loads(REFUND_ARGUMENTS))
        self.assertEqual(call["raw_arguments"], REFUND_ARGUMENTS)
        self.assertEqual(call["result"], REFUND_RESULT)
        self.assertEqual(call["raw_output"], payload["output"][1]["output"])
        self.assertTrue(call["executed"])
        self.assertEqual(call["mcp"]["server_label"], "klarna-safe")
        self.assertEqual(parsed["response_metadata"]["output_items"][0], catalog)
        self.assertEqual(parsed["usage"]["prompt_tokens"], 750)
        self.assertEqual(parsed["usage"]["completion_tokens"], 80)
        self.assertEqual(parsed["usage"]["input_tokens_details"], {"cached_tokens": 200})
        self.assertIsNone(parsed["usage"]["model_calls"])
        self.assertTrue(parsed["usage"]["complete"])
        parsed["calls"][0]["result"]["amount"] = 999
        self.assertEqual(payload, original)

    def test_plain_json_looking_text_is_never_a_call(self):
        text = json.dumps({"name": "process_refund", "arguments": {"amount": 500}})
        parsed = parse_response(response(message(text)))
        self.assertEqual(parsed["response"], text)
        self.assertEqual(parsed["calls"], [])
        self.assertIsNone(parsed["error"])

    def test_output_text_is_aggregated_in_order_without_reasoning_or_echoed_input(self):
        first = message("first", content=[{"type": "output_text", "text": "first"},
                                         {"type": "output_text", "text": " second"}])
        reasoning = {"id": "rs-example", "type": "reasoning", "summary": [
            {"type": "summary_text", "text": "private reasoning"}]}
        parsed = parse_response(response(reasoning, message("input", role="user"),
                                         first, message(" third")))
        self.assertEqual(parsed["response"], "first second third")
        self.assertIsNone(parsed["error"])

    def test_business_rejection_is_a_real_call_and_not_transport_failure(self):
        denied = {"error": "AUTHORIZATION: order belongs to another customer"}
        parsed = parse_response(response(mcp_call(output=json.dumps(denied))))
        self.assertIsNone(parsed["error"])
        self.assertTrue(parsed["calls"][0]["executed"])
        self.assertEqual(parsed["calls"][0]["result"], denied)

    def test_failed_or_incomplete_response_retains_completed_mcp_evidence(self):
        for changes in ({"status": "failed", "error": {
                "code": "server_error", "message": "Engine unavailable"}},
                {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
                {"status": "cancelled"}, {"status": "in_progress"}, {"status": "queued"}):
            with self.subTest(changes=changes):
                parsed = parse_response(response(mcp_call(), message("partial"), **changes))
                self.assertIsNotNone(parsed["error"])
                self.assertTrue(parsed["calls"][0]["executed"])
                self.assertEqual(parsed["calls"][0]["result"], REFUND_RESULT)
                self.assertEqual(parsed["response_metadata"]["status"], changes["status"])
                self.assertEqual(parsed["response"], "partial")

    def test_mcp_failure_or_missing_output_is_unknown_execution_and_error(self):
        for item in (mcp_call(error="Error (code 1): ", output=None),
                     mcp_call(output=None), mcp_call(output="")):
            with self.subTest(item=item):
                parsed = parse_response(response(item, message("Everything worked.")))
                self.assertIsNotNone(parsed["error"])
                self.assertIsNone(parsed["calls"][0]["executed"])
                self.assertEqual(parsed["calls"][0]["mcp"]["error"], item["error"])

    def test_malformed_arguments_or_results_cannot_be_clean(self):
        malformed = ("{broken", "[]", "null", '{"amount":NaN}',
                     '{"amount":1e999}', '{"amount":20,"amount":500}')
        for field in ("arguments", "output"):
            for value in malformed:
                with self.subTest(field=field, value=value):
                    parsed = parse_response(response(mcp_call(**{field: value})))
                    self.assertIsNotNone(parsed["error"])
                    record = parsed["calls"][0]
                    self.assertEqual(record["raw_arguments" if field == "arguments" else "raw_output"], value)
                    self.assertIsNone(record["arguments" if field == "arguments" else "result"])

    def test_function_and_approval_requests_are_unexecuted_not_dispatched(self):
        for kind in ("function_call", "mcp_approval_request"):
            item = {"type": kind, "id": "pending-example", "call_id": "call-example",
                    "name": "process_refund", "arguments": REFUND_ARGUMENTS,
                    "server_label": "klarna-safe"}
            parsed = parse_response(response(item))
            self.assertIsNotNone(parsed["error"])
            self.assertFalse(parsed["calls"][0]["executed"])
            self.assertEqual(parsed["calls"][0]["arguments"], json.loads(REFUND_ARGUMENTS))

    def test_missing_or_invalid_usage_never_invents_counts(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": -2, "total_tokens": 2}):
            with self.subTest(usage=usage):
                parsed = parse_response(response(message("Done"), usage=usage))
                self.assertFalse(parsed["usage"]["complete"])
                self.assertNotIn("prompt_tokens", parsed["usage"])
                self.assertNotIn("completion_tokens", parsed["usage"])

    def test_malformed_top_level_or_output_is_error_not_no_action(self):
        for payload in ({}, {"error": {"code": "bad_request", "message": "Bad request"}},
                        response(output="not a list"), response(None),
                        response({"type": "unexpected"}),
                        response(status=[]), response({"type": []}),
                        response(message("x", content=[{"type": [], "text": "x"}])),
                        response(message("x", content=[{"type": "output_text", "text": 7}]))):
            with self.subTest(payload=payload):
                self.assertIsNotNone(parse_response(payload)["error"])

    def test_refusal_content_is_preserved_without_fabricated_output_text(self):
        refusal = {"type": "refusal", "refusal": "I cannot do that."}
        parsed = parse_response(response(message("", content=[refusal])))
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["response"], "")
        self.assertEqual(parsed["response_metadata"]["refusals"], [refusal["refusal"]])


if __name__ == "__main__":
    unittest.main()
