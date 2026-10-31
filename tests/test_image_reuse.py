"""Exact image/context preservation and HTTP transport checks; no model calls."""

import json
import unittest
from unittest import mock
from urllib.error import HTTPError

from auriga.core.presentation.image_reuse import reuse_exact_images
from auriga.cortex.session.model_proxy import ModelRequestProxy
import test_model_proxy as proxy_fixture


class ReuseTests(unittest.TestCase):
    @staticmethod
    def image(value="aaa", **kwargs):
        return dict(type="input_image", image_url="data:image/webp;base64," + value, **kwargs)

    def test_keeps_first_and_distinct_images_without_mutation(self):
        first, changed = self.image(), self.image("aab")
        request = dict(input=[
            dict(role="user", content=[first, changed]),
            dict(type="custom_tool_call_output", output=[first, changed, first]),
        ])
        original = json.dumps(request)
        result, stats = reuse_exact_images(request)
        self.assertEqual(json.dumps(request), original)
        self.assertEqual(stats["replaced"], 3)
        self.assertEqual(result["input"][0]["content"], [first, changed])
        self.assertIn("image 2", result["input"][1]["output"][1]["text"])

    def test_detail_is_part_of_identity(self):
        request = dict(input=[dict(content=[self.image(detail="high"), self.image(detail="low")])])
        result, stats = reuse_exact_images(request)
        self.assertEqual(result, request)
        self.assertEqual(stats["replaced"], 0)

    def test_no_cross_request_state_or_text_schema_rewrite(self):
        image = self.image()
        request = dict(input=[dict(content=[image]), dict(type="function_call", arguments=json.dumps(image))],
                       tools=[dict(content=[image, image])])
        for _ in range(2):
            result, stats = reuse_exact_images(request)
            self.assertEqual(result, request)
            self.assertEqual(stats["images"], 1)

    def test_compacted_context_retains_first_available_image(self):
        image = self.image()
        request = dict(input=[dict(type="custom_tool_call_output", output=[image])])
        result, stats = reuse_exact_images(request)
        self.assertEqual(result["input"][0]["output"], [image])
        self.assertEqual(stats["replaced"], 0)

    def test_external_file_images_are_retained_and_counted_for_references(self):
        file_image = dict(type="input_image", file_id="file-123")
        external_image = dict(type="input_image", image_url="https://example.invalid/image.png")
        image = self.image()
        request = dict(input=[dict(content=[file_image, file_image, external_image, image, image])])
        result, stats = reuse_exact_images(request)
        self.assertEqual(result["input"][0]["content"][:4], request["input"][0]["content"][:4])
        self.assertIn("image 4", result["input"][0]["content"][4]["text"])
        self.assertEqual(stats["replaced"], 1)


class ProxyImageReuseTests(unittest.TestCase):
    # Reuse only the HTTP fixture lifecycle, without importing/discovering its cases twice.
    setUp = proxy_fixture.ModelProxyTest.setUp
    tearDown = proxy_fixture.ModelProxyTest.tearDown
    request = staticmethod(proxy_fixture.ModelProxyTest.request)

    @staticmethod
    def payload():
        image = dict(type="input_image", image_url="data:image/png;base64,AAA")
        return json.dumps(dict(model="fixture", input=[dict(role="user", content=[image, image])])).encode()

    def test_disabled_is_byte_identical(self):
        payload = self.payload()
        with ModelRequestProxy(self.upstream) as proxy:
            self.request(proxy, payload)
        self.assertEqual(self.requests[0][1], payload)
        self.assertFalse(any(event["kind"] == "model_images_reused" for event in proxy.events))

    def test_enabled_reuses_and_records_without_image_payload(self):
        payload = self.payload()
        with ModelRequestProxy(self.upstream, image_reuse=True) as proxy:
            self.request(proxy, payload)
        sent = json.loads(self.requests[0][1])["input"][0]["content"]
        self.assertEqual(sent[0]["type"], "input_image")
        self.assertEqual(sent[1]["type"], "input_text")
        event = next(event for event in proxy.events if event["kind"] == "model_images_reused")
        self.assertEqual(event["replaced"], 1)
        self.assertNotIn("data:image", json.dumps(event))
        self.assertEqual(event["forwarded_body_bytes"], len(self.requests[0][1]))

    def test_compaction_bypasses_rewrite(self):
        self.behavior = "compact"
        payload = self.payload()
        with ModelRequestProxy(self.upstream, image_reuse=True) as proxy:
            self.request(proxy, payload, path="/responses/compact")
        self.assertEqual(self.requests[0][1], payload)

    def test_one_image_remains_byte_identical(self):
        value = json.loads(self.payload())
        value["input"][0]["content"].pop()
        payload = json.dumps(value, indent=2).encode()
        with ModelRequestProxy(self.upstream, image_reuse=True) as proxy:
            self.request(proxy, payload)
        self.assertEqual(self.requests[0][1], payload)

    def test_automatic_error_retry_reuses_same_transformed_body_and_charges_one_completion(self):
        self.script = ["503", "complete"]
        with ModelRequestProxy(self.upstream, image_reuse=True, max_requests=1) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                self.request(proxy, self.payload())
            self.assertFalse(proxy.transport_blocked)
            self.assertEqual(proxy.attempt_count, 2)
            self.assertEqual(proxy.request_count, 1)
            with self.assertRaises(HTTPError) as error:
                self.request(proxy, self.payload())
            self.assertEqual(error.exception.code, 429)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0][1], self.requests[1][1])
        self.assertEqual(sum(event["kind"] == "model_images_reused" for event in proxy.events), 1)
        self.assertEqual(sum(event["kind"] == "model_request_retry" for event in proxy.events), 1)


if __name__ == "__main__":
    unittest.main()
