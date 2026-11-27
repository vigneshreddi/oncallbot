import copy
import importlib.util
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("chaos_runner", ROOT / "chaos/runner/chaos_runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.faults = runner.load_catalog(ROOT / "chaos/faults.yaml")

    def test_six_workflows(self):
        self.assertEqual(6, len(self.faults))
        for fault in self.faults:
            events = []
            def respond(request):
                if request.url.path == "/payments/admin/policy":
                    return httpx.Response(200, json={"capture_mode": "CAPTURED"})
                status = 500 if request.url.path == "/orders" or "/placement/" in request.url.path else 200
                return httpx.Response(status, json={})
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                event = runner.run_scenario(fault, client, events.append, duration=0)
            self.assertTrue(event["cleanup_ok"])
            self.assertEqual(1, len(events))
            self.assertEqual(len(fault["cleanup"]), sum(result["phase"] == "cleanup" for result in event["requests"]))

    def test_partial_prepare_failure_still_cleans_all(self):
        fault = self.faults[1]
        events = []
        def respond(request):
            if request.url.query == b"tier=slow":
                return httpx.Response(503)
            return httpx.Response(200)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(RuntimeError):
                runner.run_scenario(fault, client, events.append, duration=0)
        self.assertEqual(2, sum(result["phase"] == "cleanup" for result in events[0]["requests"]))
        self.assertTrue(events[0]["cleanup_ok"])

    def test_cleanup_failure_retries_and_continues(self):
        events = []
        def respond(request):
            if request.url.path.endswith("/tier/delete"):
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(500 if request.url.path == "/orders" else 200)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(RuntimeError):
                runner.run_scenario(self.faults[1], client, events.append, duration=0)
        self.assertFalse(events[0]["cleanup_ok"])
        self.assertTrue(events[0]["requests"][-1]["path"].endswith("SKU-CHAOS-RETRY/delete"))
        self.assertEqual(3, sum(result["path"].endswith("tier/delete") for result in events[0]["requests"]))

    def test_capture_mode_restored_and_missing_snapshot_skipped(self):
        for snapshot_ok in (True, False):
            events = []
            def respond(request):
                if request.url.path.endswith("/policy"):
                    return httpx.Response(200 if snapshot_ok else 503, json={"capture_mode": "CUSTOM"})
                return httpx.Response(500 if request.url.path == "/orders" else 200)
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                if snapshot_ok:
                    runner.run_scenario(self.faults[2], client, events.append, duration=0)
                else:
                    with self.assertRaises(RuntimeError):
                        runner.run_scenario(self.faults[2], client, events.append, duration=0)
            cleanup = [result for result in events[0]["requests"] if result["phase"] == "cleanup"]
            if snapshot_ok:
                self.assertIn("mode=CUSTOM", cleanup[0]["path"])
            else:
                self.assertEqual("no captured state", cleanup[0]["skipped"])

    def test_transport_error_only_accepted_in_provoke(self):
        events = []
        def respond(request):
            if request.url.path == "/orders":
                raise httpx.ReadTimeout("timeout", request=request)
            return httpx.Response(200)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            runner.run_scenario(self.faults[1], client, events.append, duration=0)
        self.assertTrue(events[0]["cleanup_ok"])

    def test_seed_weights_exclusion_and_empty_selection(self):
        first = random.Random("42")
        second = random.Random("42")
        self.assertEqual([runner.choose_fault(self.faults, first)["id"] for _ in range(30)],
                         [runner.choose_fault(self.faults, second)["id"] for _ in range(30)])
        faults = copy.deepcopy(self.faults)
        for fault in faults[1:]:
            fault["weight"] = 0
        self.assertEqual(faults[0], runner.choose_fault(faults, first))
        with self.assertRaises(ValueError):
            runner.choose_fault(faults, first, {faults[0]["id"]})

    def test_dry_run_no_http(self):
        def forbidden(request):
            self.fail("Dry-run attempted HTTP")
        with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
            for fault in self.faults:
                runner.run_scenario(fault, client, lambda event: None, dry_run=True)

    def test_event_is_private_action_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            runner.record_event({"fault_id": "sample", "cleanup_ok": False}, path)
            self.assertIn('"cleanup_ok": false', path.read_text())
        source = (ROOT / "chaos/runner/chaos_runner.py").read_text()
        self.assertNotIn("psycopg", source)
        self.assertNotIn("expected_root_cause", (ROOT / "chaos/faults.yaml").read_text())

    def test_slow_charge_uses_numeric_unique_fixture_and_matching_cleanup(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            runner.run_scenario(self.faults[5], client, lambda event: None, duration=0)
        order_id = json.loads(requests[1].content)["orderId"]
        self.assertIsInstance(order_id, int)
        self.assertLess(order_id, 0)
        self.assertEqual(f"/payments/admin/charges/{order_id}/delete", requests[-1].url.path)

    def test_interrupt_runs_cleanup_and_records(self):
        stop = threading.Event()
        events = []
        def respond(request):
            stop.set()
            return httpx.Response(200)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(RuntimeError):
                runner.run_scenario(self.faults[0], client, events.append, stop=stop, duration=0)
        self.assertTrue(events[0]["cleanup_ok"])
        self.assertEqual("cleanup", events[0]["requests"][-1]["phase"])

    def test_disabled_default_does_not_load_catalog_or_send_http(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(runner, "load_catalog") as load:
            runner.main([])
            load.assert_not_called()

    def test_manifest_disabled_and_no_investigator_or_database_access(self):
        manifest = yaml.safe_load((ROOT / "deploy/k8s/chaos.yaml").read_text())
        self.assertEqual(0, manifest["spec"]["replicas"])
        self.assertEqual("Recreate", manifest["spec"]["strategy"]["type"])
        template = manifest["spec"]["template"]
        self.assertNotIn("incidentlab/access-postgres", template["metadata"]["labels"])
        container = template["spec"]["containers"][0]
        environment = {entry["name"]: entry for entry in container["env"]}
        self.assertEqual("false", environment["CHAOS_ENABLED"]["value"])
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("PGPASSWORD", environment)
        self.assertFalse(template["spec"]["automountServiceAccountToken"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])

    def test_invalid_snapshot_cannot_change_or_restore_policy(self):
        events = []
        def respond(request):
            return httpx.Response(200, json={"capture_mode": None})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(ValueError):
                runner.run_scenario(self.faults[2], client, events.append, duration=0)
        self.assertFalse(any("mode=AUTHORIZED" in result["path"] for result in events[0]["requests"]))
        self.assertEqual("no captured state", events[0]["requests"][1]["skipped"])


if __name__ == "__main__":
    unittest.main()