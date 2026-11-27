import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from investigator import api, config, sources, store, tools, vendors


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.enterContext(patch.object(config, "services", return_value=[{"name": "orders"}, {"name": "payments"}]))
        self.upsert = self.enterContext(patch.object(store, "upsert_incident", return_value=(7, True)))
        self.evidence = self.enterContext(patch.object(store, "add_evidence", autospec=True))
        self.seen = self.enterContext(patch.object(store, "occurrence_incident", return_value=None))
        self.occurrence = self.enterContext(patch.object(store, "add_occurrence", return_value=True))
        self.schedule = self.enterContext(patch.object(api, "on_new_incident", return_value=True))

    def datadog(self, **changes):
        return {"alert_id": "123", "monitor_name": "Failed orders", "id": "event-1",
                "alert_status": "Alert", "alert_transition": "Triggered", "event_type": "monitor_alert",
                "tags": "env:prod,service:orders", "date": "1791028800000", "trace_id": "abc", **changes}

    def test_datadog_maps_monitor_service_tags_event_time_and_deduplicates(self):
        response = self.client.post("/webhooks/datadog", json=self.datadog())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"incident_id": 7, "created": True})
        event = self.evidence.call_args.args[3]
        self.assertEqual(event["service"], "orders")
        self.assertEqual(event["event_at"], "2026-10-03T12:00:00+00:00")
        self.assertEqual(event["metadata"]["monitor_id"], "123")
        self.assertEqual(event["metadata"]["alert_status"], "Alert")
        self.assertEqual(self.schedule.call_args.args[-1], "abc")
        fingerprint = self.upsert.call_args.args[0]
        self.upsert.return_value = (7, False)
        response = self.client.post("/webhooks/datadog", json=self.datadog())
        self.assertEqual(self.upsert.call_args.args[0], fingerprint)
        self.assertFalse(response.json()["created"])
        self.schedule.assert_called_once()
        self.evidence.assert_called_once()

    def test_datadog_rejects_ambiguous_unknown_or_missing_service_monitor_time_and_urls(self):
        for changes in ({"tags": ["service:orders", "service:payments"]}, {"tags": []},
                        {"tags": "service:unknown"}, {"service": "payments"}, {"alert_id": None},
                        {"date": "not-time"}, {"date": True}, {"date": 0},
                        {"url": "https://example.invalid"}, {"tags": ["x"] * 65}):
            with self.subTest(changes=changes):
                response = self.client.post("/webhooks/datadog", json=self.datadog(**changes))
                self.assertEqual(response.status_code, 422)
        self.upsert.assert_not_called()
        self.schedule.assert_not_called()

    def test_datadog_explicit_service_list_tags_and_new_event_identity(self):
        response = self.client.post("/webhooks/datadog", json=self.datadog(service="orders", tags=["env:prod"]))
        self.assertEqual(response.status_code, 200)
        original = self.upsert.call_args.args[0]
        self.client.post("/webhooks/datadog", json=self.datadog(id="event-2", date="1791028900000"))
        self.assertEqual(self.upsert.call_args.args[0], original)
        self.assertEqual([call.kwargs["external_id"] for call in self.occurrence.call_args_list], ["event-1", "event-2"])
        self.client.post("/webhooks/datadog", json=self.datadog(monitor_id="other-monitor", id="event-3"))
        self.assertNotEqual(self.upsert.call_args.args[0], original)

    def test_same_error_from_different_alerts_joins_one_incident_with_correlation_ids(self):
        text = '{"message": "Order 1503 failed: java.lang.NullPointerException at Place", "error.type": "java.lang.NullPointerException", "dd.span_id": "s-1", "correlation_id": "c-1"}'
        self.client.post("/webhooks/datadog", json=self.datadog(id="a", text=text, tags=["service:orders", "request_id:r-1"]))
        first = self.upsert.call_args.args[0]
        other = text.replace("1503", "2201").replace("s-1", "s-2")
        self.client.post("/webhooks/datadog", json=self.datadog(id="b", monitor_id="different-monitor", text=other, trace_id="t-2"))
        self.assertEqual(self.upsert.call_args.args[0], first)
        ids = [call.kwargs["ids"] for call in self.occurrence.call_args_list]
        self.assertEqual(ids[0], {"span_id": "s-1", "correlation_id": "c-1", "request_id": "r-1", "trace_id": "abc"})
        self.assertEqual((ids[1]["trace_id"], ids[1]["span_id"]), ("t-2", "s-2"))
        self.assertEqual(sources.issue_fingerprint("orders", "Order 1503 failed: java.lang.NullPointerException at Place",
                                                   "java.lang.NullPointerException"), first)

    def test_redelivered_alert_is_not_counted_twice(self):
        self.seen.return_value = 7
        response = self.client.post("/webhooks/datadog", json=self.datadog())
        self.assertEqual(response.json(), {"incident_id": 7, "created": False, "duplicate": True})
        self.upsert.assert_not_called()
        self.occurrence.assert_not_called()
        self.schedule.assert_not_called()

    def test_loki_alerts_and_splunk_export_alert_mapping_do_not_fetch_links(self):
        with patch.object(vendors.httpx.Client, "stream") as request:
            response = self.client.post("/webhooks/loki", json={"status": "firing", "alerts": [{
                "labels": {"service": "orders", "alertname": "errors", "trace_id": "abc"},
                "annotations": {"summary": "Failed orders"}, "startsAt": "2026-10-03T12:00:00Z",
                "fingerprint": "alert-1", "generatorURL": "https://example.invalid"}]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["incidents"][0]["incident_id"], 7)
            response = self.client.post("/webhooks/splunk", json={"search_name": "Failed orders", "sid": "search-1",
                "results_link": "https://example.invalid", "result": {"service": "orders", "_time": "1791028800",
                "_raw": '{"trace.id":"abc"}'}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.schedule.call_args.args[-1], "abc")
            request.assert_not_called()

    def test_vendor_capacity_failure_and_generic_webhook_remains_available(self):
        self.schedule.return_value = False
        response = self.client.post("/webhooks/datadog", json=self.datadog())
        self.assertEqual(response.status_code, 503)
        response = self.client.post("/webhooks/incident", json={"service": "orders", "title": "external"})
        self.assertEqual(response.status_code, 503)

    def test_api_bounds_vendor_failures_and_tools_forward_window(self):
        source = MagicMock()
        source.search.return_value = [{"message": "failed"}]
        with patch.object(api, "source", source):
            response = self.client.get("/logs", params={"service": "orders", "text": "failed", "limit": 10, "time_window": 60})
            self.assertEqual(response.status_code, 200)
            source.search.assert_called_once_with("orders", "failed", 10, time_window=60)
            for params in ({"limit": 201}, {"limit": 0}, {"time_window": 86401}, {"time_window": 0}):
                self.assertEqual(self.client.get("/logs", params=params).status_code, 422)
            source.search.side_effect = vendors.VendorError("Log provider request failed")
            self.assertEqual(self.client.get("/logs").status_code, 502)
        with patch.object(tools, "logs", source):
            source.search.side_effect = None
            tools.search_logs("orders", "failed", 999, 60)
            source.search.assert_called_with("orders", "failed", 100, time_window=60)
            tools.get_log_context("abc", 999, 60)
            source.get_context.assert_called_with("abc", 200, time_window=60)
        schema = next(item for item in tools.TOOL_SCHEMAS if item["name"] == "search_logs")["parameters"]
        self.assertNotIn("url", schema["properties"])
        self.assertFalse(schema["additionalProperties"])

    def test_only_kubernetes_starts_collector(self):
        with patch.object(store, "ensure_schema"), patch.object(api, "index_status", return_value=[]), \
             patch.object(api.kube_config, "load_incluster_config") as load, patch.object(api.threading, "Thread") as thread:
            for provider in (vendors.DatadogLogSource, vendors.LokiLogSource, vendors.SplunkLogSource):
                with patch.object(api, "source", MagicMock(spec=provider)):
                    api.startup()
            load.assert_not_called()
            thread.assert_not_called()
            with patch.object(api, "source", sources.KubernetesLogSource()):
                api.startup()
            load.assert_called_once()
            thread.return_value.start.assert_called_once()

    def test_kubernetes_queries_bound_time_and_limit_without_changing_record_shape(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = [{"service": "orders", "raw": {}}]
        with patch.object(store, "db", return_value=conn):
            source = sources.KubernetesLogSource()
            rows = source.search("orders", "failed", 999, 60)
            sql, params = conn.execute.call_args.args
            self.assertIn("between %s and %s", sql)
            self.assertEqual((params[1] - params[0]).total_seconds(), 60)
            self.assertEqual(params[-1], 200)
            self.assertEqual(rows[0]["raw"], {})
            source.get_context("abc", 999, 60)
            sql, params = conn.execute.call_args.args
            self.assertEqual(params[0], "abc")
            self.assertEqual((params[2] - params[1]).total_seconds(), 60)


if __name__ == "__main__":
    unittest.main()