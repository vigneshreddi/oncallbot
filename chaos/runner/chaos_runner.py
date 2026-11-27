"""Execute catalog HTTP workflows; private JSONL events never enter investigator storage."""
import argparse
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import tempfile
import threading
import time
from urllib.parse import quote
import uuid

import httpx
import yaml

SERVICES = {"order-service", "inventory-service", "payment-service"}
PHASES = ("prepare", "provoke", "cleanup")
DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "faults.yaml"


def load_catalog(path):
    with open(path, encoding="utf-8") as source:
        catalog = yaml.safe_load(source)
    if catalog.get("version") != 2:
        raise ValueError("Expected fault catalog version 2")
    faults = catalog["faults"]
    if not faults or len({fault["id"] for fault in faults}) != len(faults):
        raise ValueError("Fault IDs must be nonempty and unique")
    for fault in faults:
        weight = fault.get("weight", 1)
        duration = fault.get("duration_seconds", 0)
        if not math.isfinite(weight) or weight < 0 or not math.isfinite(duration) or duration < 0:
            raise ValueError("Weights and durations must be finite and nonnegative")
        for phase in PHASES:
            if not fault.get(phase):
                raise ValueError(f"Missing {phase} requests for {fault['id']}")
            for step in fault[phase]:
                if step["service"] not in SERVICES or step["method"] not in {"GET", "POST"}:
                    raise ValueError("Unsupported service or method")
                if not step["path"].startswith("/") or step["path"].startswith("//"):
                    raise ValueError("Requests must use service-relative paths")
    return faults


def choose_fault(faults, rng, excluded=()):
    eligible = [fault for fault in faults if fault.get("chaos", False)
                and fault["id"] not in excluded and fault.get("weight", 1) > 0]
    if not eligible:
        raise ValueError("No eligible chaos scenarios")
    return rng.choices(eligible, weights=[fault.get("weight", 1) for fault in eligible], k=1)[0]


def record_event(event, path):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event) + "\n")


def render_json(value, context):
    if isinstance(value, dict):
        return {key: render_json(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_json(item, context) for item in value]
    if isinstance(value, str):
        match = re.fullmatch(r"\{\{(\w+)\}\}", value)
        if match:
            return context[match[1]]
    return value


def execute_step(client, step, phase, context, base_urls, results, dry_run=False):
    if step.get("requires") and step["requires"] not in context:
        results.append({"phase": phase, "path": step["path"], "skipped": "no captured state"})
        return
    path = re.sub(r"\{\{(\w+)\}\}", lambda match: quote(str(context[match[1]]), safe=""), step["path"])
    url = base_urls.get(step["service"], f"http://{step['service']}:8080").rstrip("/") + path
    result = {"phase": phase, "method": step["method"], "service": step["service"], "path": path}
    results.append(result)
    if dry_run:
        result["dry_run"] = True
        context.update({name: "captured-value" for name in step.get("save", {})})
        print(f"{phase}: {step['method']} {url}", flush=True)
        return
    started = time.monotonic()
    try:
        response = client.request(step["method"], url, json=render_json(step.get("json"), context), timeout=15)
        result["status"] = response.status_code
        expected = step.get("expected_status", list(range(200, 300))) if phase == "provoke" else range(200, 300)
        if response.status_code not in expected:
            raise RuntimeError(f"Unexpected HTTP {response.status_code} during {phase}")
        if step.get("save"):
            payload = response.json()
            captured = {name: payload[field] for name, field in step["save"].items()}
            if any(not isinstance(value, str) for value in captured.values()):
                raise ValueError("Captured policy values must be strings")
            context.update(captured)
    except httpx.TransportError as error:
        result["error"] = type(error).__name__
        if phase != "provoke" or not step.get("allow_transport_error", False):
            raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)


def run_scenario(fault, client, recorder, *, base_urls=None, dry_run=False, stop=None, duration=None):
    stop = stop or threading.Event()
    context = {"charge_order_id": -(uuid.uuid4().int % (2**63 - 1) + 1)}
    event = {"fault_id": fault["id"], "started_at": time.time(), "requests": [], "cleanup_errors": []}
    failure = None
    try:
        for phase in ("prepare", "provoke"):
            for step in fault[phase]:
                if stop.is_set():
                    raise RuntimeError("Scenario interrupted")
                execute_step(client, step, phase, context, base_urls or {}, event["requests"], dry_run)
        if not dry_run:
            stop.wait(fault.get("duration_seconds", 0) if duration is None else duration)
    except BaseException as error:
        failure = error
        event["error"] = type(error).__name__
    finally:
        for step in fault["cleanup"]:
            for attempt in range(3):
                try:
                    execute_step(client, step, "cleanup", context, base_urls or {}, event["requests"], dry_run)
                    break
                except Exception as error:
                    if attempt == 2:
                        event["cleanup_errors"].append({"path": step["path"], "error": type(error).__name__})
        event["finished_at"] = time.time()
        event["cleanup_ok"] = not event["cleanup_errors"]
        recorder(event)
    if failure is not None:
        raise failure
    if event["cleanup_errors"]:
        raise RuntimeError("Cleanup incomplete; see private event record")
    return event


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=os.getenv("FAULTS_PATH", str(DEFAULT_CATALOG)))
    parser.add_argument("--fault", help="Explicitly run one scenario, regardless of CHAOS_ENABLED")
    parser.add_argument("--base-url", action="append", default=[], metavar="SERVICE=URL")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("CHAOS_DRY_RUN", "false").lower() == "true")
    parser.add_argument("--seed", default=os.getenv("CHAOS_SEED"))
    parser.add_argument("--duration", type=float)
    parser.add_argument("--events", default=os.getenv("CHAOS_EVENTS_PATH", str(Path(tempfile.gettempdir()) / "chaos-events.jsonl")))
    args = parser.parse_args(argv)
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration < 0):
        parser.error("--duration must be finite and nonnegative")
    if not args.fault and not args.dry_run and os.getenv("CHAOS_ENABLED", "false").lower() != "true":
        print("Chaos disabled (CHAOS_ENABLED defaults to false)", flush=True)
        return
    faults = load_catalog(args.catalog)
    base_urls = dict(value.split("=", 1) for value in args.base_url)
    if any(service not in SERVICES or not url.startswith(("http://", "https://")) for service, url in base_urls.items()):
        parser.error("--base-url requires a known service and an HTTP(S) URL")
    rng = random.Random(args.seed or None)
    excluded = {value.strip() for value in os.getenv("CHAOS_EXCLUDE", "").split(",")}
    selected = next((fault for fault in faults if fault["id"] == args.fault), None)
    if args.fault and selected is None:
        parser.error("Unknown fault")
    cooldown = float(os.getenv("CHAOS_COOLDOWN_SECONDS", "120"))
    if not math.isfinite(cooldown) or cooldown < 0:
        parser.error("CHAOS_COOLDOWN_SECONDS must be finite and nonnegative")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with httpx.Client() as client:
        while not stop.is_set():
            fault = selected or choose_fault(faults, rng, excluded)
            run_scenario(fault, client, lambda event: record_event(event, args.events),
                         base_urls=base_urls, dry_run=args.dry_run, stop=stop, duration=args.duration)
            if selected or args.dry_run:
                return
            stop.wait(cooldown)


if __name__ == "__main__":
    main()
