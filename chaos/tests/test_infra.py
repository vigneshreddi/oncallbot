import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import yaml

SPEC = importlib.util.spec_from_file_location("infra_runner", Path(__file__).resolve().parents[1] / "runner/infra_runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class InfraTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.client = Mock(context="test-context")
        self.client.get.return_value = {"metadata": {"uid": "original", "labels": {"incidentlab/infra-fault": "true"}}, "spec": {"replicas": 0}}
        self.client.pods.return_value = []
        self.stop = Mock()
        self.stop.is_set.return_value = False

    def run_fault(self, scenario="scale-zero", service="order-service"):
        runner.run_scenario(self.client, scenario, service, 5, self.path, self.stop)

    def test_scale_restores_original_and_journals_before_mutation(self):
        self.client.get.return_value["spec"]["replicas"] = 3
        def scale(kind, name, replicas):
            self.assertEqual(3, json.loads(self.path.read_text())["replicas"])
        self.client.scale.side_effect = scale
        self.run_fault()
        self.assertEqual([0, 3], [call.args[2] for call in self.client.scale.call_args_list])
        self.assertFalse(self.path.exists())

    def test_partial_mutation_failure_restores(self):
        self.client.scale.side_effect = [RuntimeError("API failure"), None]
        with self.assertRaises(RuntimeError):
            self.run_fault()
        self.assertEqual([0, 0], [call.args[2] for call in self.client.scale.call_args_list])
        self.assertFalse(self.path.exists())

    def test_cleanup_failure_retains_recovery_journal(self):
        self.client.scale.side_effect = [None, RuntimeError(), RuntimeError(), RuntimeError()]
        with self.assertRaises(RuntimeError):
            self.run_fault()
        self.assertTrue(self.path.exists())
        self.client.scale.side_effect = None
        runner.restore(self.client, self.path)
        self.assertFalse(self.path.exists())

    def test_existing_journal_blocks_new_run(self):
        self.path.write_text('{}')
        with self.assertRaises(FileExistsError):
            self.run_fault()
        self.client.get.assert_not_called()

    def test_disallowed_targets_never_mutate(self):
        for scenario, service in [("scale-zero", "postgres"), ("scale-zero", "investigator"), ("node-drain", None), ("oom", "order-service")]:
            with self.subTest(scenario=scenario, service=service), self.assertRaises(ValueError):
                self.run_fault(scenario, service)
        self.client.scale.assert_not_called()

    def test_nonzero_isolated_workload_refused(self):
        self.client.get.return_value["spec"]["replicas"] = 1
        with self.assertRaises(ValueError):
            self.run_fault("oom", None)
        self.client.scale.assert_not_called()

    def test_oom_requires_real_termination_reason_and_restores(self):
        pods = [{"metadata": {"name": "oom-pod"}, "status": {"containerStatuses": [{"lastState": {"terminated": {"reason": "OOMKilled"}}}]}}]
        self.client.pods.side_effect = [pods, []]
        self.run_fault("oom", None)
        self.assertEqual([1, 0], [call.args[2] for call in self.client.scale.call_args_list])
        self.assertFalse(runner.oom_evidence([{"status": {"containerStatuses": [{"state": {"terminated": {"reason": "Error", "exitCode": 137}}}]}}]))

    def test_simulated_node_requires_affinity_evidence(self):
        pods = [{"status": {"conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "node affinity mismatch"}]}}]
        self.client.pods.side_effect = [pods, []]
        self.run_fault("node-unavailable-simulated", None)
        self.assertEqual([1, 0], [call.args[2] for call in self.client.scale.call_args_list])
        self.client.call.assert_not_called()

    def test_pod_delete_replacement_and_restore(self):
        pod = {"metadata": {"name": "demo-fault-pod-0", "uid": "old-pod",
                            "ownerReferences": [{"uid": "original", "controller": True}]},
               "status": {"phase": "Running"}}
        replacement = {"metadata": {"name": "demo-fault-pod-0", "uid": "replacement"}}
        self.client.pods.side_effect = [[pod], [replacement], []]
        self.run_fault("pod-delete", None)
        self.client.call.assert_called_once_with("delete", "pod", "demo-fault-pod-0", "--wait=false")
        self.assertEqual([1, 0], [call.args[2] for call in self.client.scale.call_args_list])
        self.assertFalse(self.path.exists())

    def test_pod_delete_wrong_owner_refused(self):
        self.client.pods.side_effect = [[{"metadata": {"name": "demo-fault-pod-0", "uid": "other", "ownerReferences": []}, "status": {"phase": "Running"}}], []]
        with self.assertRaises(ValueError):
            self.run_fault("pod-delete", None)
        self.client.call.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_evidence_timeout_still_restores(self):
        with patch.object(runner.time, "monotonic", side_effect=[0, 6, 7]):
            with self.assertRaisesRegex(RuntimeError, "evidence"):
                self.run_fault("oom", None)
        self.assertEqual([1, 0], [call.args[2] for call in self.client.scale.call_args_list])
        self.assertFalse(self.path.exists())

    def test_signal_interruption_restores(self):
        self.stop.is_set.side_effect = [False, True]
        with self.assertRaises(InterruptedError):
            self.run_fault("oom", None)
        self.assertFalse(self.path.exists())
        self.assertEqual(0, self.client.scale.call_args.args[2])

    def test_uid_and_context_mismatch_refuse_restore(self):
        self.client.scale.side_effect = [None, RuntimeError(), RuntimeError(), RuntimeError()]
        with self.assertRaises(RuntimeError):
            self.run_fault()
        self.client.scale.reset_mock(side_effect=True)
        self.client.get.return_value["metadata"]["uid"] = "replacement"
        with self.assertRaises(ValueError):
            runner.restore(self.client, self.path)
        self.client.scale.assert_not_called()
        self.client.context = "other-context"
        with self.assertRaises(ValueError):
            runner.restore(self.client, self.path)

    def test_duration_bounds(self):
        for duration in [0, 121, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                runner.run_scenario(self.client, "oom", None, duration, self.path, threading.Event())
        self.client.get.assert_not_called()

    def test_dry_run_and_disabled_have_no_api_calls(self):
        with patch.object(runner.subprocess, "run") as execute:
            runner.main(["--context", "test"])
            runner.main(["--context", "test", "--dry-run", "--scenario", "oom"])
        execute.assert_not_called()

    def test_kubectl_always_scoped_and_impersonated(self):
        with patch.object(runner.subprocess, "run", return_value=Mock(stdout='{}')) as execute:
            runner.Kubectl("test").get("deployment", "order-service")
        command = execute.call_args.args[0]
        self.assertIn(runner.IDENTITY, command)
        self.assertIn(runner.NAMESPACE, command)
        self.assertEqual(20, execute.call_args.kwargs["timeout"])


class ManifestTests(unittest.TestCase):
    def load(self, name):
        path = Path(__file__).resolve().parents[2] / "deploy/k8s" / name
        return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))

    def test_rbac_cannot_mutate_nodes_database_or_investigator(self):
        documents = self.load("infra-chaos-rbac.yaml")
        self.assertEqual(["ServiceAccount", "Role", "RoleBinding"], [document["kind"] for document in documents])
        for document in documents:
            self.assertEqual(runner.NAMESPACE, document["metadata"]["namespace"])
        for rule in documents[1]["rules"]:
            self.assertFalse({"nodes", "secrets", "pods/exec", "pods/eviction"} & set(rule["resources"]))
            self.assertFalse({"create", "deletecollection", "*"} & set(rule["verbs"]))
            if set(rule["verbs"]) & {"patch", "update", "delete"}:
                self.assertTrue(rule["resourceNames"])
                self.assertFalse({"postgres", "investigator"} & set(rule["resourceNames"]))
                if "delete" in rule["verbs"]:
                    self.assertEqual(["demo-fault-pod-0"], rule["resourceNames"])
                    self.assertEqual(["pods"], rule["resources"])
                else:
                    self.assertTrue(all(resource.endswith("/scale") for resource in rule["resources"]))

    def test_workloads_disabled_hardened_and_oom_exceeds_limit(self):
        documents = self.load("demo-fault-workloads.yaml")
        self.assertEqual(3, len(documents))
        for document in documents:
            self.assertIn(document["kind"], {"Deployment", "StatefulSet"})
            self.assertEqual(0, document["spec"]["replicas"])
            spec = document["spec"]["template"]["spec"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertTrue(spec["securityContext"]["runAsNonRoot"])
            self.assertEqual("RuntimeDefault", spec["securityContext"]["seccompProfile"]["type"])
            self.assertFalse(spec.get("hostNetwork", False))
            self.assertTrue(all("emptyDir" in volume and "hostPath" not in volume for volume in spec["volumes"]))
            for container in spec["containers"]:
                self.assertFalse(container.get("ports"))
                self.assertNotIn(":latest", container["image"])
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                self.assertFalse(container["securityContext"].get("privileged", False))
                self.assertEqual(["ALL"], container["securityContext"]["capabilities"]["drop"])
                self.assertIn("memory", container["resources"]["limits"])
        oom = documents[0]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual("64Mi", oom["resources"]["limits"]["memory"])
        self.assertIn("range(12)", oom["args"][0])
        self.assertIn("bytearray(8 * 1024 * 1024)", oom["args"][0])
        affinity = documents[1]["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]
        term = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]
        self.assertEqual(["incidentlab-simulated-nonexistent-node"], term["matchFields"][0]["values"])


if __name__ == "__main__":
    unittest.main()