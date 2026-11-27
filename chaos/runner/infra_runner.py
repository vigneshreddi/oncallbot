"""Opt-in, namespace-scoped infrastructure faults. No HTTP control surface."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

NAMESPACE = "incident-lab"
IDENTITY = "system:serviceaccount:incident-lab:infra-chaos"
SERVICES = {"order-service", "inventory-service", "payment-service"}
ISOLATED = {"demo-fault-oom", "demo-fault-node"}
SCENARIOS = ("scale-zero", "pod-delete", "oom", "node-unavailable-simulated")


class Kubectl:
    def __init__(self, context):
        if not context or context.startswith("-"):
            raise ValueError("An explicit Kubernetes context is required")
        self.context = context

    def call(self, *args):
        result = subprocess.run(
            ["kubectl", "--context", self.context, "--namespace", NAMESPACE,
             "--as", IDENTITY, "--request-timeout=10s", *args],
            capture_output=True, text=True, check=True, timeout=20,
        )
        return json.loads(result.stdout) if result.stdout.strip().startswith("{") else None

    def get(self, kind, name):
        return self.call("get", kind, name, "-o", "json")

    def scale(self, kind, name, replicas):
        self.call("scale", f"{kind}/{name}", f"--replicas={replicas}")

    def pods(self, name):
        return self.call("get", "pods", "-l", f"app={name}", "-o", "json")["items"]


def target_for(scenario, service):
    if scenario == "scale-zero":
        if service not in SERVICES:
            raise ValueError("scale-zero requires an allowlisted demo service")
        return "deployment", service
    if service is not None:
        raise ValueError("--service is only valid for scale-zero")
    targets = {"pod-delete": ("statefulset", "demo-fault-pod"),
               "oom": ("deployment", "demo-fault-oom"),
               "node-unavailable-simulated": ("deployment", "demo-fault-node")}
    if scenario not in targets:
        raise ValueError("Actual node drain/failure is unsupported; requires an operator")
    return targets[scenario]


def persist(path, state):
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def validate_state(state, context):
    kind, name = target_for(state["scenario"], state.get("service"))
    replicas = state["replicas"]
    if (state.get("version") != 1 or state.get("context") != context
            or state.get("namespace") != NAMESPACE
            or state.get("kind") != kind or state.get("name") != name
            or type(replicas) is not int or replicas < 0
            or not isinstance(state.get("uid"), str) or not state["uid"]):
        raise ValueError("Invalid recovery journal or mismatched context")
    if name not in SERVICES and replicas != 0:
        raise ValueError("Isolated workloads must originally be disabled")


def restore(client, path):
    state = json.loads(path.read_text(encoding="utf-8"))
    validate_state(state, client.context)
    kind, name = state["kind"], state["name"]
    for attempt in range(3):
        try:
            current = client.get(kind, name)
            if current["metadata"]["uid"] != state["uid"]:
                raise ValueError("Target was replaced; operator recovery required")
            client.scale(kind, name, state["replicas"])
            deadline = time.monotonic() + 60
            while True:
                current = client.get(kind, name)
                if current["metadata"]["uid"] != state["uid"]:
                    raise ValueError("Target was replaced during recovery")
                restored = current["spec"].get("replicas", 1) == state["replicas"]
                if name not in SERVICES:
                    restored = restored and not client.pods(name)
                if restored:
                    path.unlink()
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError("Restoration confirmation timed out")
                threading.Event().wait(1)
        except ValueError:
            raise
        except Exception:
            if attempt == 2:
                raise


def observe(client, name, predicate, stop, deadline):
    while not stop.is_set():
        pods = client.pods(name)
        if predicate(pods):
            return pods
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Expected Kubernetes evidence was not observed")
        stop.wait(min(1, remaining))
    raise InterruptedError("Infrastructure scenario interrupted")


def oom_evidence(pods):
    return any(termination.get("reason") == "OOMKilled"
               for pod in pods for status in pod.get("status", {}).get("containerStatuses", [])
               for termination in (status.get("lastState", {}).get("terminated", {}),
                                   status.get("state", {}).get("terminated", {})))


def unschedulable_evidence(pods):
    return any(condition.get("type") == "PodScheduled"
               and condition.get("status") == "False"
               and condition.get("reason") == "Unschedulable"
               and "affinity" in condition.get("message", "").lower()
               for pod in pods for condition in pod.get("status", {}).get("conditions", []))


def run_scenario(client, scenario, service, duration, path, stop):
    if not math.isfinite(duration) or not 5 <= duration <= 120:
        raise ValueError("Duration must be between 5 and 120 seconds")
    kind, name = target_for(scenario, service)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as reservation:
        reservation.write('{}')
        reservation.flush()
        os.fsync(reservation.fileno())
    journaled = False
    try:
        original = client.get(kind, name)
        state = {"version": 1, "context": client.context, "namespace": NAMESPACE,
                 "scenario": scenario, "service": service, "kind": kind, "name": name,
                 "uid": original["metadata"]["uid"], "replicas": original["spec"].get("replicas", 1)}
        validate_state(state, client.context)
        if name not in SERVICES and original["metadata"].get("labels", {}).get("incidentlab/infra-fault") != "true":
            raise ValueError("Target is not an isolated fault workload")
        if stop.is_set():
            raise InterruptedError("Interrupted before mutation")
        persist(path, state)
        journaled = True
        deadline = time.monotonic() + duration
        client.scale(kind, name, 0 if scenario == "scale-zero" else 1)
        if scenario == "scale-zero":
            observe(client, name, lambda pods: not pods, stop, deadline)
            print(f"Demo service {name} has zero pods; original scale will be restored")
        elif scenario == "oom":
            pods = observe(client, name, oom_evidence, stop, deadline)
            print(json.dumps({"scenario": scenario, "evidence": "OOMKilled", "pods": [pod["metadata"]["name"] for pod in pods]}))
        elif scenario == "node-unavailable-simulated":
            observe(client, name, unschedulable_evidence, stop, deadline)
            print("SIMULATED node unavailability: isolated pod has unsatisfiable node affinity; no node changed")
        elif scenario == "pod-delete":
            pods = observe(client, name, lambda items: any(
                pod["metadata"]["name"] == "demo-fault-pod-0"
                and pod.get("status", {}).get("phase") == "Running" for pod in items), stop, deadline)
            pod = next(item for item in pods if item["metadata"]["name"] == "demo-fault-pod-0")
            if not any(owner.get("uid") == state["uid"] and owner.get("controller")
                       for owner in pod["metadata"].get("ownerReferences", [])):
                raise ValueError("Pod is not controlled by the isolated StatefulSet")
            if stop.is_set():
                raise InterruptedError("Interrupted before pod deletion")
            client.call("delete", "pod", "demo-fault-pod-0", "--wait=false")
            observe(client, name, lambda items: any(
                item["metadata"]["name"] == "demo-fault-pod-0"
                and item["metadata"]["uid"] != pod["metadata"]["uid"] for item in items), stop, deadline)
            print("Isolated pod deleted and replacement observed")
        stop.wait(max(0, deadline - time.monotonic()))
    finally:
        if journaled:
            restore(client, path)
        else:
            path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--service", choices=sorted(SERVICES))
    parser.add_argument("--context", required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--state", default=str(Path(__file__).resolve().parents[2] / "tmp/infra-chaos-state.json"))
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)
    client = Kubectl(args.context)
    if args.restore:
        if args.scenario or args.service or args.dry_run:
            parser.error("--restore cannot be combined with a scenario or dry-run")
        restore(client, Path(args.state))
        return
    if not args.enable and not args.dry_run:
        print("Infrastructure chaos disabled; use --enable explicitly")
        return
    kind, name = target_for(args.scenario, args.service)
    if not math.isfinite(args.duration) or not 5 <= args.duration <= 120:
        parser.error("--duration must be 5..120 seconds")
    if args.dry_run:
        print(json.dumps({"scenario": args.scenario, "namespace": NAMESPACE,
                          "kind": kind, "target": name, "duration": args.duration,
                          "simulated": args.scenario == "node-unavailable-simulated"}))
        return
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    run_scenario(client, args.scenario, args.service, args.duration, args.state, stop)


if __name__ == "__main__":
    main()