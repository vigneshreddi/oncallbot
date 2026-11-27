import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from investigator import config, sources, vendors


class VendorTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.payload = {}
        self.status = 200
        self.body = None
        for name, value in {"DATADOG_API_KEY": str(id(self)), "DATADOG_APP_KEY": str(id(self)),
                            "DATADOG_SITE": "datadoghq.com", "LOKI_BASE_URL": "https://loki.example.invalid",
                            "LOKI_USERNAME": "", "LOKI_PASSWORD": "", "LOKI_TOKEN": "", "LOKI_TENANT_ID": "",
                            "SPLUNK_BASE_URL": "https://splunk.example.invalid:8089", "SPLUNK_TOKEN": str(id(self)),
                            "SPLUNK_INDEX": "main"}.items():
            self.enterContext(patch.object(config, name, value))
        self.enterContext(patch.object(config, "services", return_value=[{"name": "orders"}, {"name": "payments"}]))
        self.transport = httpx.MockTransport(self.respond)

    def respond(self, request):
        self.requests.append(request)
        if self.body is not None:
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.payload)

    def test_datadog_request_bounds_literals_normalization_and_no_pagination(self):
        self.payload = {"data": [{"attributes": {"service": "orders", "status": "error", "message": "failed",
                        "timestamp": "2026-10-03T12:00:00Z", "host": "pod", "attributes": {
                            "trace": {"id": "abc"}, "error.stack_trace": "ExampleException"}}}],
                        "meta": {"page": {"after": "ignored"}}}
        source = vendors.DatadogLogSource(self.transport)
        rows = source.search("orders", 'failed" OR *', 10000, 1000000)
        request = self.requests[0]
        self.assertEqual((request.method, request.url.path), ("POST", "/api/v2/logs/events/search"))
        body = json.loads(request.content)
        self.assertEqual(body["page"], {"limit": 200})
        self.assertEqual((datetime.fromisoformat(body["filter"]["to"]) - datetime.fromisoformat(body["filter"]["from"])).total_seconds(), 86400)
        self.assertIn('"failed\\" OR \\*"', body["filter"]["query"])
        self.assertEqual(rows[0]["trace_id"], "abc")
        self.assertEqual(rows[0]["level"], "ERROR")
        self.assertEqual(rows[0]["raw"]["error.stack_trace"], "ExampleException")
        self.assertEqual(len(self.requests), 1)
        source.get_context("abc", 0, 0)
        body = json.loads(self.requests[-1].content)
        self.assertEqual(body["page"]["limit"], 1)
        self.assertEqual(body["sort"], "timestamp")
        self.assertIn('@trace.id:"abc"', body["filter"]["query"])

    def test_loki_json_and_plain_logs_global_sort_and_trace(self):
        self.payload = {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": "orders", "pod": "pod"}, "values": [
                ["1791028800000000000", '{"message":"failed","log.level":"ERROR","trace.id":"abc"}']]},
            {"stream": {"service": "payments"}, "values": [["1791028801000000000", "plain line"]]}]}}
        source = vendors.LokiLogSource(self.transport)
        rows = source.search(None, 'a" |= "b', 2, 60)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/loki/api/v1/query_range")
        self.assertEqual(request.url.params["limit"], "2")
        self.assertEqual(int(request.url.params["end"]) - int(request.url.params["start"]), 60_000_000_000)
        self.assertIn("|= " + json.dumps('a" |= "b'), request.url.params["query"])
        self.assertEqual([row["message"] for row in rows], ["plain line", "failed"])
        self.assertEqual(rows[1]["trace_id"], "abc")
        self.assertEqual(source.get_context("abc")[0]["pod"], "pod")
        self.assertIn('trace_id="abc"', self.requests[-1].url.params["query"])
        self.assertEqual(self.requests[-1].url.params["direction"], "forward")

    def test_splunk_single_synchronous_export_normalizes_raw_and_bounds(self):
        self.body = (json.dumps({"preview": False, "result": {"service": "orders", "_time": "1791028800",
                     "_raw": '{"message":"failed","traceId":"abc","level":"error"}'}}) + "\n").encode()
        source = vendors.SplunkLogSource(self.transport)
        rows = source.search("orders", 'x" | delete', 500, 60)
        request = self.requests[0]
        body = parse_qs(request.content.decode())
        self.assertEqual((request.method, request.url.path), ("POST", "/services/search/jobs/export"))
        self.assertEqual(body["exec_mode"], ["oneshot"])
        self.assertEqual(body["max_count"], ["200"])
        self.assertEqual(float(body["latest_time"][0]) - float(body["earliest_time"][0]), 60)
        self.assertIn('"x\\" | delete"', body["search"][0])
        self.assertTrue(body["search"][0].endswith("| head 200"))
        self.assertEqual(rows[0]["trace_id"], "abc")
        self.assertEqual(rows[0]["message"], "failed")
        self.assertEqual(rows[0]["level"], "ERROR")
        self.assertEqual(len(self.requests), 1)

    def test_reject_insecure_urls_and_invalid_site_or_index(self):
        for value in ("http://example.invalid", "https://user:pass@example.invalid", "https://example.invalid?q=x",
                      "https://example.invalid/#fragment", "https://example.invalid:bad"):
            with self.subTest(value=value), patch.object(config, "LOKI_BASE_URL", value):
                with self.assertRaises(ValueError):
                    vendors.LokiLogSource(self.transport)
        with patch.object(config, "DATADOG_SITE", "example.invalid"):
            with self.assertRaises(ValueError):
                vendors.DatadogLogSource(self.transport)
        with patch.object(config, "SPLUNK_INDEX", "main | delete"):
            with self.assertRaises(ValueError):
                vendors.SplunkLogSource(self.transport)

    def test_unknown_service_and_control_characters_never_make_requests(self):
        source = vendors.DatadogLogSource(self.transport)
        for args in (("https://example.invalid", "text"), ("orders", "text\nsearch")):
            with self.assertRaises(ValueError):
                source.search(*args)
        self.assertFalse(self.requests)

    def test_status_redirect_size_invalid_json_and_transport_errors_are_sanitized(self):
        source = vendors.DatadogLogSource(self.transport)
        for status in (302, 401, 429, 503):
            self.status = status
            with self.assertRaises(vendors.VendorError) as error:
                source.search()
            self.assertNotIn(config.DATADOG_API_KEY, str(error.exception))
        self.status = 200
        self.body = b"x" * (vendors.MAX_RESPONSE_BYTES + 1)
        with self.assertRaisesRegex(vendors.VendorError, "byte limit"):
            source.search()
        self.body = b"invalid"
        with self.assertRaisesRegex(vendors.VendorError, "invalid JSON"):
            source.search()
        with patch.object(httpx.Client, "stream", side_effect=httpx.ConnectError(config.DATADOG_API_KEY)):
            with self.assertRaisesRegex(vendors.VendorError, "request failed") as error:
                source.search()
        self.assertNotIn(config.DATADOG_API_KEY, str(error.exception))

    def test_factory_selects_configured_provider_and_default_is_kubernetes(self):
        for name, expected in (("kubernetes", sources.KubernetesLogSource), ("datadog", vendors.DatadogLogSource),
                               ("loki", vendors.LokiLogSource), ("splunk", vendors.SplunkLogSource)):
            with patch.object(config, "LOG_PROVIDER", name):
                self.assertIsInstance(sources.make_log_source(), expected)
        with patch.object(config, "LOG_PROVIDER", "unknown"):
            with self.assertRaises(ValueError):
                sources.make_log_source()

    def test_tls_redirect_policy_deadline_and_malformed_provider_responses(self):
        source = vendors.DatadogLogSource(self.transport)
        self.payload = {"data": []}
        original = httpx.Client
        with patch.object(vendors.httpx, "Client", wraps=original) as client:
            source.search()
        self.assertTrue(client.call_args.kwargs["verify"])
        self.assertFalse(client.call_args.kwargs["follow_redirects"])
        self.assertFalse(client.call_args.kwargs["trust_env"])
        with patch.object(vendors.time, "monotonic", side_effect=[0, config.LOG_HTTP_TIMEOUT_SECONDS + 1]):
            with self.assertRaisesRegex(vendors.VendorError, "deadline"):
                source.search()
        for provider, payload in ((vendors.DatadogLogSource, {}), (vendors.DatadogLogSource, {"data": [None]}),
                                  (vendors.LokiLogSource, {"status": "success", "data": {"resultType": "matrix"}})):
            self.payload = payload
            with self.assertRaises(vendors.VendorError):
                provider(self.transport).search()
        self.body = b'{"messages":[{"type":"ERROR","text":"private details"}]}\n'
        with self.assertRaisesRegex(vendors.VendorError, "export failed"):
            vendors.SplunkLogSource(self.transport).search()
        self.assertIsNone(vendors.event_time("invalid"))
        self.assertEqual(vendors.normalize({"service": {"name": "orders"}})["service"], "orders")

    def test_authentication_headers_and_missing_credentials(self):
        self.payload = {"status": "success", "data": {"resultType": "streams", "result": []}}
        with patch.object(config, "LOKI_TOKEN", str(id(self))), patch.object(config, "LOKI_TENANT_ID", "tenant"):
            vendors.LokiLogSource(self.transport).search()
        self.assertTrue(self.requests[-1].headers["Authorization"].startswith("Bearer "))
        self.assertEqual(self.requests[-1].headers["X-Scope-OrgID"], "tenant")
        with patch.object(config, "LOKI_USERNAME", "reader"), patch.object(config, "LOKI_PASSWORD", str(id(self))):
            vendors.LokiLogSource(self.transport).search()
        self.assertTrue(self.requests[-1].headers["Authorization"].startswith("Basic "))
        for name, provider in (("DATADOG_API_KEY", vendors.DatadogLogSource), ("SPLUNK_TOKEN", vendors.SplunkLogSource)):
            with patch.object(config, name, ""), self.assertRaises(ValueError):
                provider(self.transport)


if __name__ == "__main__":
    unittest.main()